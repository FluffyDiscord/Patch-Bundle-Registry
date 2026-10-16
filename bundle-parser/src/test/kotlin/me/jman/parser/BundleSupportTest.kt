package me.jman.parser

import java.io.File
import java.io.IOException
import kotlin.io.path.createTempDirectory
import kotlin.test.*

class BundleSupportTest {
    @Test fun pinnedCompilerDependenciesAreHashedCachedAndRestrictedToTheirArtifacts() {
        val root = createTempDirectory("bundle-support").toFile()
        try {
            val bytes = File(root,"bytes").apply { writeText("verified compiler dependency") }
            val hash = sha256(bytes)
            val config = RuntimeConfiguration(WorkerSettings(),emptyList(),bundle_support=listOf(
                BundleSupport(listOf("a".repeat(64)),listOf(RuntimeExecutable("https://example.test/base.rvp",hash)))
            ))
            var calls = 0
            val downloader: (java.net.URL,File) -> Unit = { _, file -> calls++; file.writeBytes(bytes.readBytes()) }
            assertTrue(supportArtifacts(config,"b".repeat(64),root,downloader).isEmpty())
            val first = supportArtifacts(config,"a".repeat(64),root,downloader).single()
            assertEquals(hash,sha256(first))
            assertEquals(first,supportArtifacts(config,"a".repeat(64),root,downloader).single())
            assertEquals(1,calls)
            first.writeText("corrupt cache")
            assertFailsWith<IOException> {
                supportArtifacts(config,"a".repeat(64),root) { _, file -> file.writeText("incorrect bytes") }
            }
            assertEquals(hash,sha256(supportArtifacts(config,"a".repeat(64),root,downloader).single()))
        } finally {root.deleteRecursively()}
    }
}
