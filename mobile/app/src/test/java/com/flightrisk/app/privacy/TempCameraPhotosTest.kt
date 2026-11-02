package com.flightrisk.app.privacy

import java.io.File
import java.nio.file.Files
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class TempCameraPhotosTest {
    private lateinit var cache: File

    @Before fun setUp() {
        cache = Files.createTempDirectory("fr-cache").toFile()
    }

    @After fun tearDown() {
        cache.deleteRecursively()
    }

    @Test fun createdFileIsDeletedAfterUse() {
        val f = TempCameraPhotos.create(cache)
        assertTrue(f.exists())
        assertTrue(f.name.startsWith("flightrisk_target_"))
        assertTrue(TempCameraPhotos.delete(f))
        assertFalse(f.exists())
    }

    @Test fun deleteOfNullOrMissingFileIsFine() {
        assertTrue(TempCameraPhotos.delete(null))
        assertTrue(TempCameraPhotos.delete(File(cache, "gone.jpg")))
    }

    @Test fun sweepRemovesOnlyStaleCaptureFiles() {
        val a = TempCameraPhotos.create(cache)
        val b = TempCameraPhotos.create(cache)
        val other = File(cache, "image_cache.bin").apply { writeText("x") }
        val otherJpg = File(cache, "thumb.jpg").apply { writeText("x") }
        val dirLike = File(cache, "flightrisk_target_dir.jpg").apply { mkdirs() }

        assertEquals(2, TempCameraPhotos.sweep(cache))

        assertFalse(a.exists())
        assertFalse(b.exists())
        assertTrue(other.exists())
        assertTrue(otherJpg.exists())
        assertTrue(dirLike.exists())
    }

    @Test fun sweepOfMissingFolderIsZero() {
        assertEquals(0, TempCameraPhotos.sweep(File(cache, "nope")))
    }

    @Test fun appSweepsAtStartAndPickerDeletesAfterDecode() {
        val app = File("src/main/java/com/flightrisk/app/FlightRiskApp.kt").readText()
        assertTrue(app.contains("TempCameraPhotos.sweep(cacheDir)"))
        val picker = File("src/main/java/com/flightrisk/app/ui/target/TargetPickerScreen.kt").readText()
        assertTrue(picker.contains("TempCameraPhotos.delete(photoFile)"))
        assertTrue(
            Regex("finally \\{\\s*//[^\\n]*\\n\\s*TempCameraPhotos\\.delete\\(photoFile\\)").containsMatchIn(picker),
        )
    }
}
