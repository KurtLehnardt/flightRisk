package com.flightrisk.app.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer

/**
 * Hostile and clean ONNX models, built in memory with a tiny protobuf encoder
 * (field numbers from onnx.proto). No ONNX library and no fixture files.
 */
class OnnxModelScannerTest {
    @get:Rule val tmp = TemporaryFolder()

    // ---- minimal protobuf encoder ----
    private fun varint(v: Long): ByteArray {
        val o = ByteArrayOutputStream()
        var x = v
        while (x and 0x7F.inv().toLong() != 0L) {
            o.write(((x and 0x7F) or 0x80).toInt())
            x = x ushr 7
        }
        o.write(x.toInt())
        return o.toByteArray()
    }

    private fun tag(field: Int, wire: Int) = varint((field.toLong() shl 3) or wire.toLong())
    private fun cat(vararg parts: ByteArray): ByteArray = parts.fold(ByteArray(0)) { a, b -> a + b }
    private fun len(field: Int, body: ByteArray) = cat(tag(field, 2), varint(body.size.toLong()), body)
    private fun str(field: Int, s: String) = len(field, s.toByteArray())
    private fun int(field: Int, v: Long) = cat(tag(field, 0), varint(v))

    /** TensorProto: name, dims, float data inline or external_data + data_location. */
    private fun tensor(external: String? = null, inline: Boolean = true): ByteArray {
        val base = cat(str(8, "w"), int(1, 2), int(2, 1))
        return if (external != null) {
            cat(base, len(13, cat(str(1, "location"), str(2, external))), int(14, 1))
        } else if (inline) {
            cat(base, len(4, ByteArray(8)), int(14, 0))
        } else base
    }

    private fun graph(vararg parts: ByteArray) = cat(str(2, "g"), *parts)
    private fun model(vararg parts: ByteArray) = cat(int(1, 8), *parts)
    private fun node(vararg attrs: ByteArray) = len(1, cat(str(4, "Op"), *attrs))
    private fun attrTensor(t: ByteArray) = len(5, cat(str(1, "value"), len(5, t)))
    private fun attrGraph(g: ByteArray) = len(5, cat(str(1, "then_branch"), len(6, g)))

    private fun scan(bytes: ByteArray) = OnnxModelScanner.scan(ByteBuffer.wrap(bytes))

    private fun refused(bytes: ByteArray) {
        val e = runCatching { scan(bytes) }.exceptionOrNull()
        assertTrue("expected UnsafeModel, got $e", e is OnnxModelScanner.UnsafeModel)
    }

    private val traversal = "../../shared_prefs/secret.xml"

    @Test
    fun `an initializer with external data that climbs out of the folder is refused`() {
        refused(model(len(7, graph(len(5, tensor(external = traversal))))))
    }

    @Test
    fun `an initializer with an absolute external path is refused`() {
        refused(model(len(7, graph(len(5, tensor(external = "/data/data/com.flightrisk.app/databases/app.db"))))))
    }

    @Test
    fun `an external initializer inside a subgraph of an If node is refused`() {
        val inner = graph(len(5, tensor(external = traversal)))
        refused(model(len(7, graph(node(attrGraph(inner))))))
    }

    @Test
    fun `an external initializer in a model function is refused`() {
        val fn = cat(str(1, "f"), len(7, cat(str(4, "Op"), attrTensor(tensor(external = traversal)))))
        refused(model(len(7, graph()), len(25, fn)))
    }

    @Test
    fun `an external sparse initializer is refused`() {
        val sparse = len(15, len(1, tensor(external = traversal)))
        refused(model(len(7, graph(sparse))))
    }

    @Test
    fun `an external tensor attribute on a node is refused`() {
        refused(model(len(7, graph(node(attrTensor(tensor(external = traversal)))))))
    }

    @Test
    fun `external data entries without data_location are refused too`() {
        val t = cat(str(8, "w"), len(13, cat(str(1, "location"), str(2, traversal))))
        refused(model(len(7, graph(len(5, t)))))
    }

    @Test
    fun `a clean model with inline data passes`() {
        val s = scan(model(len(7, graph(node(), len(5, tensor())))))
        assertEquals(1, s.graphs)
        assertEquals(1, s.nodes)
        assertEquals(1, s.tensors)
    }

    @Test
    fun `a clean model with a tensor in a subgraph passes`() {
        val s = scan(model(len(7, graph(node(attrGraph(graph(len(5, tensor()))))))))
        assertEquals(2, s.graphs)
    }

    @Test
    fun `a file without a graph is refused`() {
        refused(model(str(2, "producer")))
    }

    @Test
    fun `a truncated file is refused`() {
        val ok = model(len(7, graph(len(5, tensor()))))
        refused(ok.copyOf(ok.size - 3))
    }

    @Test
    fun `a model nested deeper than the limit is refused`() {
        var g = graph()
        repeat(OnnxModelScanner.MAX_DEPTH) { g = graph(node(attrGraph(g))) }
        refused(model(len(7, g)))
    }

    @Test
    fun `scanning a file on disk gives the same verdict`() {
        val bad = File(tmp.root, "bad.onnx").apply { writeBytes(model(len(7, graph(len(5, tensor(external = traversal)))))) }
        val good = File(tmp.root, "good.onnx").apply { writeBytes(model(len(7, graph(len(5, tensor()))))) }
        assertTrue(runCatching { OnnxModelScanner.scan(bad) }.exceptionOrNull() is OnnxModelScanner.UnsafeModel)
        assertEquals(1, OnnxModelScanner.scan(good).tensors)
    }
}
