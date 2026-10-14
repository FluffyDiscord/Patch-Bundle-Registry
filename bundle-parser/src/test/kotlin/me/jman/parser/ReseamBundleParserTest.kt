package me.jman.parser

import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.*
import kotlin.test.*

class ReseamPatchParserTest {
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
        val result = parseReseamInspection(inspection("[$patch]"), "hash", "runtime")
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
        assertEquals(0, parseReseamInspection(inspection(), "hash", "runtime").patches?.size)
        val hidden = patch.replace("\"hidden\":false", "\"hidden\":true")
        assertEquals(1, parseReseamInspection(inspection("[$patch,$hidden]"), "hash", "runtime").patches?.size)
        val universal = JsonObject(Json.parseToJsonElement(patch).jsonObject +
            ("compatibility" to buildJsonObject { put("kind", "universal") })).toString()
        val mapped = parseReseamInspection(inspection("[$universal]"), "hash", "runtime").patches!!.single()
        assertEquals(JsonNull, mapped.jsonObject["compatiblePackages"])
    }

    @Test fun failedInspectionIsNeverASuccessfulEmptyCatalog() {
        val rejected = parseReseamInspection(inspection(problem = "{\"kind\":\"invalid_signature\"}"), "hash", "runtime")
        assertEquals("runtime_parsing_failure", rejected.status)
        assertTrue(rejected.terminal)
        assertNull(rejected.patches)
        assertFails { parseReseamInspection("{\"patches\":[]}", "hash", "runtime") }
        assertFails { parseReseamInspection(inspection().replace("a".repeat(64), "bad"), "hash", "runtime") }
    }

    @Test fun engineRequirementReordersRemainingRuntimesWithoutDroppingFallbacks() {
        val config = Json.decodeFromString<RuntimeConfiguration>(
            java.io.File("../config/patcher-runtimes.json").readText())
        val candidates = selectRuntimes(config, "Reseam", null).drop(1)
        val selected = prioritizeReseamEngine(candidates, "0.18.1")
        assertEquals("app.reseam:reseam:0.18.3", selected.first().coordinate)
        assertEquals("app.reseam:reseam:0.12.0",
            prioritizeReseamEngine(candidates, "0.12.0").first().coordinate)
        assertEquals(candidates.map { it.id }.toSet(), selected.map { it.id }.toSet())
        assertEquals(candidates.size, selected.size)
        for (required in listOf("", "0.1.0", "0.7.0")) {
            assertEquals("reseam-jvm", prioritizeReseamEngine(candidates, required).first().loader)
        }
        assertEquals(candidates, prioritizeReseamEngine(candidates, "broken"))
        val problem = """{"type":"bundle_too_old","built":"0.18.1","running":"0.23.1"}"""
        val rejected = parseReseamInspection(inspection(problem = problem), "hash", "runtime")
        assertEquals("0.18.1", rejected.requiredEngine)
        assertNull(rejected.patches)
    }

    @Test fun configuredNativeLoaderAndLegacyFamilyMigration() {
        val config = Json.decodeFromString<RuntimeConfiguration>(
            java.io.File("../config/patcher-runtimes.json").readText())
        val runtimes = selectRuntimes(config, "Reseam", null)
        val native = runtimes.first()
        assertEquals("reseam", native.loader)
        assertEquals(setOf("linux-x64", "windows-x64"), native.executables.keys)
        assertTrue(runtimes.any { it.loader == "reseam-jvm" })
        assertTrue(runtimes.filter { it.loader == "reseam" }.all { "linux-x64" in it.executables })
        for (version in listOf("0.8.1", "0.12.1", "0.16.0", "0.18.3", "0.20.2", "0.22.0")) {
            assertTrue(runtimes.any { it.coordinate == "app.reseam:reseam:$version" })
        }
        assertEquals("ReVanced:v21", canonicalBundleFamily("ReVanced:V3"))
        assertEquals("ReVanced:v22", canonicalBundleFamily("ReVanced:V4"))
        assertEquals("Morphe:V1", canonicalBundleFamily("Morphe:V1"))
        assertEquals("Reseam", canonicalBundleFamily("ReSeam"))
    }
}
