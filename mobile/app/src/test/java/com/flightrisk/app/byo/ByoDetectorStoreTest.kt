package com.flightrisk.app.byo

import com.flightrisk.app.llm.FakePrefs
import com.flightrisk.app.vision.UltralyticsContract.Layout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** The imported detector's file and record, on a temp folder and in-memory preferences. */
class ByoDetectorStoreTest {
    @get:Rule val tmp = TemporaryFolder()

    private val prefs = FakePrefs()
    private fun store() = ByoDetectorStore(tmp.root, prefs)

    private fun importOne(s: ByoDetectorStore) {
        s.folder.mkdirs()
        s.modelFile.writeBytes(ByteArray(16))
        s.save(ByoDetectorStore.Imported("abc", 16, Layout.ANCHORS))
    }

    @Test
    fun `an imported model is current until it is turned off`() {
        val s = store()
        importOne(s)
        assertNotNull(s.current())
    }

    @Test
    fun `turning off deletes the imported file and its folder`() {
        val s = store()
        importOne(s)
        s.turnOff()
        assertFalse(s.modelFile.exists())
        assertFalse(s.folder.exists())
    }

    @Test
    fun `turning off also deletes leftover import copies and the record`() {
        val s = store()
        importOne(s)
        s.newTempFile().writeBytes(ByteArray(4))
        s.turnOff()
        assertFalse(s.folder.exists())
        assertTrue(prefs.map.isEmpty())
        assertNull(s.current())
    }

    @Test
    fun `turning off with nothing imported does not fail`() {
        store().turnOff()
        assertNull(store().current())
    }

    @Test
    fun `a record without its file is not current`() {
        val s = store()
        importOne(s)
        s.modelFile.delete()
        assertNull(s.current())
    }

    @Test
    fun `stale import copies are deleted and the model is kept`() {
        val s = store()
        importOne(s)
        val stale = s.newTempFile().apply { writeBytes(ByteArray(4)) }
        s.deleteStaleImports()
        assertFalse(stale.exists())
        assertTrue(s.modelFile.exists())
        assertEquals("abc", s.current()?.sha256)
    }
}
