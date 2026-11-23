package com.flightrisk.app.vision

import com.flightrisk.app.vision.DetectorFactory.Choice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** DetectorFactory.select: which detector a search gets, with fakes for the models. */
class DetectorFactorySelectTest {
    @Test
    fun `an imported detector that fails to open falls back to YOLOX-S and says so`() {
        val r = DetectorFactory.select(true, { throw IllegalStateException("bad model") }, { "yolox" })
        assertEquals("yolox", r.detector)
        assertEquals(Choice.IMPORTED_FAILED, r.choice)
    }

    @Test
    fun `an imported detector that runs out of memory also falls back`() {
        val r = DetectorFactory.select(true, { throw OutOfMemoryError() }, { "yolox" })
        assertEquals("yolox", r.detector)
        assertEquals(Choice.IMPORTED_FAILED, r.choice)
    }

    @Test
    fun `an imported detector that opens is used and YOLOX-S is not opened`() {
        var yoloxOpened = false
        val r = DetectorFactory.select(true, { "imported" }, { yoloxOpened = true; "yolox" })
        assertEquals("imported", r.detector)
        assertEquals(Choice.IMPORTED, r.choice)
        assertEquals(false, yoloxOpened)
    }

    @Test
    fun `YOLOX-S is used when the imported detector is off and it is not opened`() {
        var importedOpened = false
        val r = DetectorFactory.select(false, { importedOpened = true; "imported" }, { "yolox" })
        assertEquals(Choice.YOLOX, r.choice)
        assertEquals(false, importedOpened)
    }

    @Test
    fun `no detector is reported when both fail`() {
        val r = DetectorFactory.select<String>(true, { throw IllegalStateException() }, { throw IllegalStateException() })
        assertNull(r.detector)
        assertEquals(Choice.NONE, r.choice)
    }

    @Test
    fun `no detector is reported when YOLOX-S is missing and nothing is imported`() {
        val r = DetectorFactory.select<String>(false, { "imported" }, { throw IllegalStateException() })
        assertNull(r.detector)
        assertEquals(Choice.NONE, r.choice)
    }
}
