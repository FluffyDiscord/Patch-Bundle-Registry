package me.jman.parser

import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.*
import kotlin.test.*

class ReSeamPatchParserTest {
    private fun inspection(patches: String = "[]", problem: String = "null") = """
        {"bundles":[{"name":"patches","author":"Example","description":"Apps",
          "public_key":"${"a".repeat(64)}","problem":$problem}],"patches":$patches}
    """.trimIndent()
    private val patch = """
        {"bundle":"patches","id":"example.ads","name":"Hide ads","description":"Remove ads",
         "hidden":false,"enabled_by_default":true,"dependencies":["patches/example.shared"],
         "compatibility":{"kind":"packages","packages":[
            {"package":"com.example","versions":[]},
            {"package":"com.other","versions":["1.0","2.0"]}]},
         "options":[{"key":"enabled","title":"Enabled","description":"Enable feature",
             "option_type":"bool","default_value":{"type":"bool","value":true},
             "valid_values":null,"required":false}]}
    """.trimIndent()

    @Test fun signedMetadataMapsPackagesOptionsAndPublisher() {
        val result = parseReSeamInspection(inspection("[$patch]"), "hash", "runtime")
        assertEquals("verified", result.status)
        assertEquals("a".repeat(64), result.reseamBundle?.get("public_key")?.jsonPrimitive?.content)
        val mapped = result.patches!!.single().jsonObject
        assertEquals("Hide ads", mapped["name"]?.jsonPrimitive?.content)
        val packages = mapped.getValue("compatiblePackages").jsonObject
        assertEquals(JsonNull, packages["com.example"])
        assertEquals(listOf("1.0", "2.0"), packages["com.other"]?.jsonArray?.map { it.jsonPrimitive.content })
        assertEquals(true, mapped["use"]?.jsonPrimitive?.boolean)
        val option = mapped.getValue("options").jsonArray.single().jsonObject
        assertEquals(true, option["default"]?.jsonPrimitive?.boolean)
    }

    @Test fun emptyCatalogAndHiddenDependenciesAreHandled() {
        assertEquals(0, parseReSeamInspection(inspection(), "hash", "runtime").patches?.size)
        val hidden = patch.replace("\"hidden\":false", "\"hidden\":true")
        assertEquals(1, parseReSeamInspection(inspection("[$patch,$hidden]"), "hash", "runtime").patches?.size)
        val universal = JsonObject(Json.parseToJsonElement(patch).jsonObject +
            ("compatibility" to buildJsonObject { put("kind", "universal") })).toString()
        val mapped = parseReSeamInspection(inspection("[$universal]"), "hash", "runtime").patches!!.single()
        assertEquals(JsonNull, mapped.jsonObject["compatiblePackages"])
    }

    @Test fun failedInspectionIsNeverASuccessfulEmptyCatalog() {
        val rejected = parseReSeamInspection(inspection(problem = "{\"kind\":\"invalid_signature\"}"), "hash", "runtime")
        assertEquals("runtime_parsing_failure", rejected.status)
        assertTrue(rejected.terminal)
        assertNull(rejected.patches)
        assertFails { parseReSeamInspection("{\"patches\":[]}", "hash", "runtime") }
        assertFails { parseReSeamInspection(inspection().replace("a".repeat(64), "bad"), "hash", "runtime") }
    }

    @Test fun configuredNativeLoaderAndLegacyFamilyMigration() {
        val config = Json.decodeFromString<RuntimeConfiguration>(
            java.io.File("../config/patcher-runtimes.json").readText())
        val native = selectRuntimes(config, "ReSeam", null).single()
        assertEquals("reseam", native.loader)
        assertEquals(setOf("linux-x64", "windows-x64"), native.executables.keys)
        assertEquals("ReVanced:v21", canonicalBundleFamily("ReVanced:V3"))
        assertEquals("ReVanced:v22", canonicalBundleFamily("ReVanced:V4"))
        assertEquals("Morphe:V1", canonicalBundleFamily("Morphe:V1"))
    }
}
