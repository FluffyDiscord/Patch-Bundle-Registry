package me.jman.parser

import java.io.File
import java.util.IdentityHashMap
import java.util.jar.JarFile
import kotlinx.serialization.json.*
import org.objectweb.asm.*

private data class PatchExport(val owner: String, val member: String, val method: Boolean)

/** Enumerate declared JVM patch exports independently of loaders that filter unnamed patches. */
private fun patchExports(file: File, namespace: String): List<PatchExport> {
    val parents = mutableMapOf<String, String?>()
    val members = mutableListOf<Pair<PatchExport, String>>()
    JarFile(file).use { jar ->
        for (entry in jar.entries().asSequence()) {
            if (!entry.name.endsWith(".class") || entry.name.startsWith("META-INF/")) continue
            jar.getInputStream(entry).use { input ->
                ClassReader(input).accept(object : ClassVisitor(Opcodes.ASM9) {
                    private lateinit var owner: String
                    override fun visit(version: Int, access: Int, name: String, signature: String?,
                                       superName: String?, interfaces: Array<out String>?) {
                        owner = name
                        parents[name] = superName
                    }
                    private fun add(access: Int, name: String, type: Type, method: Boolean) {
                        if (access and Opcodes.ACC_PUBLIC != 0 && access and Opcodes.ACC_STATIC != 0 &&
                            type.sort == Type.OBJECT)
                            members += PatchExport(owner.replace('/', '.'), name, method) to type.internalName
                    }
                    override fun visitField(access: Int, name: String, descriptor: String,
                                            signature: String?, value: Any?): FieldVisitor? {
                        add(access, name, Type.getType(descriptor), false)
                        return null
                    }
                    override fun visitMethod(access: Int, name: String, descriptor: String,
                                             signature: String?, exceptions: Array<out String>?): MethodVisitor? {
                        if (Type.getArgumentTypes(descriptor).isEmpty())
                            add(access, name, Type.getReturnType(descriptor), true)
                        return null
                    }
                }, ClassReader.SKIP_CODE or ClassReader.SKIP_DEBUG or ClassReader.SKIP_FRAMES)
            }
        }
    }
    val base = namespace + "/patch/"
    fun patchType(name: String): Boolean {
        var current: String? = name
        val seen = mutableSetOf<String>()
        while (current != null && seen.add(current)) {
            if (current in setOf(base+"Patch", base+"BytecodePatch", base+"ResourcePatch", base+"RawResourcePatch")) return true
            current = parents[current]
        }
        return false
    }
    return members.filter { patchType(it.second) }.map { it.first }
}

internal fun declaredPatchExportCount(file: File, namespace: String): Int = patchExports(file, namespace).size

internal fun completePatchMetadata(
    file: File, loader: ClassLoader, namespace: String, loaded: List<Any>,
    convert: (Any) -> JsonObject
): JsonArray {
    val patches = mutableListOf<JsonObject>()
    val converted = IdentityHashMap<Any, JsonObject>()
    for (patch in loaded) {
        if (!converted.containsKey(patch)) {
            val metadata = convert(patch)
            converted[patch] = metadata
            patches += metadata
        }
    }
    // Match loader results to exports by occurrence, rather than collapsing
    // distinct patches whose public metadata happens to be identical.
    val unmatchedLoaded = patches.groupingBy { it }.eachCount().toMutableMap()
    val seenExports = IdentityHashMap<Any, Boolean>()
    for (export in patchExports(file, namespace)) {
        try {
            val owner = loader.loadClass(export.owner)
            val patch = if (export.method) owner.getMethod(export.member).invoke(null)
                else owner.getField(export.member).get(null)
            if (patch == null) throw IncompletePatchInventory("Patch export returned null")
            if (seenExports.put(patch, true) != null) continue
            val metadata = converted.getOrPut(patch) { convert(patch) }
            val matches = unmatchedLoaded[metadata] ?: 0
            if (matches > 0) unmatchedLoaded[metadata] = matches - 1
            else patches += metadata
        } catch (error: Exception) {
            throw IncompletePatchInventory("Could not inspect " + export.owner + "." + export.member).apply { initCause(error) }
        }
    }
    return JsonArray(patches)
}
