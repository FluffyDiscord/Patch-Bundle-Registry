package me.jman.parser

import kotlinx.serialization.json.*

/**
 * Independent adapter for Reseam's published CLI JSON protocol; no upstream code is copied.
 * https://git.reseam.app/reseam/reseam/src/commit/b0f1fcba5f4e681e5526c267eae453242c21ca8f/crates/model/src/sdk.rs
 * https://git.reseam.app/reseam/reseam/src/commit/b0f1fcba5f4e681e5526c267eae453242c21ca8f/crates/model/src/patch.rs
 */
internal fun parseReseamInspection(payload: String, hash: String, runtime: String): RuntimeResult {
    val response = Json.parseToJsonElement(payload).jsonObject
    val bundles = response.getValue("bundles").jsonArray
    require(bundles.size == 1) { "Expected one inspected bundle" }
    val bundle = bundles.single().jsonObject
    if (bundle["problem"] != null && bundle["problem"] != JsonNull) {
        val problem = bundle["problem"] as? JsonObject
        val kind = (problem?.get("type") as? JsonPrimitive)?.contentOrNull
        val required = if (kind in setOf("bundle_too_old", "bundle_too_new", "engine_too_old"))
            (problem?.get("built") as? JsonPrimitive)?.contentOrNull else null
        return RuntimeResult(null, "runtime_parsing_failure", hash, terminal = true, requiredEngine = required)
    }
    fun JsonObject.text(key: String): String {
        val value = getValue(key).jsonPrimitive
        require(value.isString)
        return value.content
    }
    val publisher = buildJsonObject {
        for (key in listOf("name", "author", "description", "public_key"))
            put(key, bundle.text(key))
    }
    require(Regex("[a-fA-F0-9]{64}").matches(publisher.getValue("public_key").jsonPrimitive.content))
    val patches = response.getValue("patches").jsonArray.map { it.jsonObject }
        .filterNot { it.getValue("hidden").jsonPrimitive.boolean }
        .map { patch ->
            val compatibility = patch.getValue("compatibility").jsonObject
            val packages = when (compatibility.text("kind")) {
                "universal" -> JsonNull
                "packages" -> compatibilityJson(compatibility.getValue("packages").jsonArray.map {
                    val entry = it.jsonObject
                    val versions = entry.getValue("versions").jsonArray
                    require(versions.all { version -> version is JsonPrimitive && version.isString })
                    entry.text("package") to if (versions.isEmpty()) JsonNull else versions
                })
                else -> error("Unsupported Reseam compatibility")
            }
            buildJsonObject {
                put("name", patch.text("name"))
                put("description", patch.text("description"))
                put("use", patch.getValue("enabled_by_default").jsonPrimitive.boolean)
                put("dependencies", patch.getValue("dependencies").jsonArray)
                put("compatiblePackages", packages)
                put("options", JsonArray(patch.getValue("options").jsonArray.map { value ->
                    val option = value.jsonObject
                    buildJsonObject {
                        for (key in listOf("key", "title", "description")) put(key, option.text(key))
                        put("required", option.getValue("required").jsonPrimitive.boolean)
                        put("type", option.text("option_type"))
                        put("default", (option["default_value"] as? JsonObject)?.get("value") ?: JsonNull)
                        val values = option["valid_values"] as? JsonArray
                        put("values", values?.let {
                            buildJsonObject { it.forEach { value -> put(value.jsonPrimitive.content, value) } }
                        } ?: JsonNull)
                    }
                }))
            }
        }
    return RuntimeResult(JsonArray(patches), "verified", hash, runtime, reseamBundle = publisher)
}

/** Supply an explicit key to older inspectors without changing their global trust store. */
internal fun reseamSigningKey(artifact: java.io.File): String? = runCatching {
    java.util.zip.ZipFile(artifact).use { archive ->
        val entry = archive.getEntry("manifest.pubkey") ?: return@use null
        require(entry.size == 32L)
        archive.getInputStream(entry).use { input ->
            val bytes = input.readNBytes(33)
            require(bytes.size == 32)
            bytes.joinToString("") { "%02x".format(it) }
        }
    }
}.getOrNull()

internal fun canonicalBundleFamily(value: String?): String? = when (value) {
    "ReVanced:V3" -> "ReVanced:v21"
    "ReVanced:V4" -> "ReVanced:v22"
    "ReSeam" -> "Reseam"
    else -> value
}
