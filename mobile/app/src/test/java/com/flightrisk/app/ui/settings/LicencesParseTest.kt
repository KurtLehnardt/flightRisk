package com.flightrisk.app.ui.settings

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class LicencesParseTest {
    @Test
    fun generatedLibraryListParsesIntoLibrariesWithLicenceText() {
        val file = File(requireNotNull(System.getProperty("flightrisk.librariesJson")))
        val libraries = LicencesText.parseLibraries(file.readText())
        assertTrue("no libraries parsed", libraries.isNotEmpty())
        assertTrue(libraries.any { lib -> lib.licenses.any { it.licenseContent?.isNotBlank() == true } })
    }

    @Test
    fun malformedJsonGivesAnEmptyList() {
        assertTrue(LicencesText.parseLibraries("not json").isEmpty())
    }
}
