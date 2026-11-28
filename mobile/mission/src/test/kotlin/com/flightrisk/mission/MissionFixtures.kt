package com.flightrisk.mission

import java.io.File

/** Locates the golden fixtures in tests/fixtures/mission_core. */
object MissionFixtures {
    val dir: File by lazy {
        val path = System.getProperty("flightrisk.fixturesDir")
            ?: error("flightrisk.fixturesDir is not set")
        File(path)
    }

    fun file(name: String): File = File(dir, name).also { check(it.isFile) { "Missing fixture: $name" } }

    fun text(name: String): String = file(name).readText(Charsets.UTF_8)
}
