package com.flightrisk.app.ui.target

import androidx.exifinterface.media.ExifInterface
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The pure parts of the API 26 and 27 photo decoding: EXIF rotation and the sample size. */
class ImageOrientationTest {

    @Test
    fun `plain rotations map to their clockwise degrees`() {
        assertEquals(0, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_NORMAL))
        assertEquals(90, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_ROTATE_90))
        assertEquals(180, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_ROTATE_180))
        assertEquals(270, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_ROTATE_270))
        assertEquals(0, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_UNDEFINED))
    }

    @Test
    fun `mirrored orientations are flagged and rotate like the specification says`() {
        assertTrue(ImageOrientation.isMirrored(ExifInterface.ORIENTATION_FLIP_HORIZONTAL))
        assertTrue(ImageOrientation.isMirrored(ExifInterface.ORIENTATION_TRANSPOSE))
        assertTrue(ImageOrientation.isMirrored(ExifInterface.ORIENTATION_FLIP_VERTICAL))
        assertTrue(ImageOrientation.isMirrored(ExifInterface.ORIENTATION_TRANSVERSE))
        assertFalse(ImageOrientation.isMirrored(ExifInterface.ORIENTATION_ROTATE_90))
        assertEquals(90, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_TRANSPOSE))
        assertEquals(270, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_TRANSVERSE))
        assertEquals(180, ImageOrientation.rotationDegrees(ExifInterface.ORIENTATION_FLIP_VERTICAL))
    }

    @Test
    fun `the sample size is a power of two that never goes below the target edge`() {
        assertEquals(1, ImageOrientation.sampleSize(1280, 720))
        assertEquals(1, ImageOrientation.sampleSize(1920, 1080))
        assertEquals(1, ImageOrientation.sampleSize(3000, 2000))
        assertEquals(2, ImageOrientation.sampleSize(4000, 3000))
        assertEquals(4, ImageOrientation.sampleSize(8000, 6000))
        assertEquals(4, ImageOrientation.sampleSize(3000, 8000))
        assertEquals(1, ImageOrientation.sampleSize(0, 0))
    }
}
