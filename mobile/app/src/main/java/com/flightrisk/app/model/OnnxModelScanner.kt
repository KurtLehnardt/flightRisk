package com.flightrisk.app.model

import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.channels.FileChannel

/**
 * Checks an ONNX file the user imported before ONNX Runtime opens it.
 *
 * An ONNX tensor can keep its data in another file ("external data": a
 * `TensorProto` with `data_location = EXTERNAL` and `external_data` entries
 * naming a path). ONNX Runtime resolves those paths when it creates the
 * session, and 1.23.2 followed `../` and absolute paths, so a crafted model
 * could read any file the app can read (its preferences, its database) into a
 * tensor. The app's own models never use external data, and neither do the
 * detector exports it accepts, so this refuses any model that does, wherever
 * the tensor is: graph initializers, sparse initializers, tensor and graph
 * attributes of nodes (subgraphs of `If`, `Loop` and `Scan` included),
 * model-local functions and training information.
 *
 * It reads the protobuf wire format directly (no ONNX library), never copies
 * the tensor data (length-delimited fields it does not need are skipped), and
 * limits the work a hostile file can cause: at most [MAX_DEPTH] nested messages,
 * [MAX_NODES] nodes and [MAX_TENSORS] tensors. A file that is not a well-formed
 * ONNX protobuf with a graph is refused too.
 */
object OnnxModelScanner {
    const val MAX_DEPTH = 32
    const val MAX_NODES = 50_000
    const val MAX_TENSORS = 50_000

    /** The largest field number protobuf allows (2^29 - 1). */
    const val MAX_FIELD = 536_870_911L

    /** Why a model was refused; [message] is shown to the user. */
    class UnsafeModel(message: String) : Exception(message)

    /** What a scan found (for tests and checks). */
    data class Summary(val nodes: Int, val tensors: Int, val graphs: Int)

    /** Scans [file] (memory-mapped, read-only). */
    fun scan(file: File): Summary =
        RandomAccessFile(file, "r").use { raf ->
            raf.channel.use { ch -> scan(ch.map(FileChannel.MapMode.READ_ONLY, 0, ch.size())) }
        }

    /** Scans the model held in [model] from its position to its limit. */
    fun scan(model: ByteBuffer): Summary {
        val s = Scan(model)
        s.message(Kind.MODEL, model.position(), model.limit(), 0)
        if (s.graphs == 0) throw UnsafeModel("The file is not an ONNX model (it has no graph).")
        return Summary(s.nodes, s.tensors, s.graphs)
    }

    private enum class Kind { MODEL, TRAINING_INFO, GRAPH, NODE, FUNCTION, ATTRIBUTE, SPARSE_TENSOR, TENSOR }

    /** The sub-messages each message kind can hold, by field number (onnx.proto). */
    private fun child(kind: Kind, field: Int): Kind? = when (kind) {
        Kind.MODEL -> when (field) {
            7 -> Kind.GRAPH // graph
            20 -> Kind.TRAINING_INFO // training_info
            25 -> Kind.FUNCTION // functions
            else -> null
        }
        Kind.TRAINING_INFO -> when (field) {
            1, 2 -> Kind.GRAPH // initialization, algorithm
            else -> null
        }
        Kind.GRAPH -> when (field) {
            1 -> Kind.NODE // node
            5 -> Kind.TENSOR // initializer
            15 -> Kind.SPARSE_TENSOR // sparse_initializer
            else -> null
        }
        Kind.NODE -> if (field == 5) Kind.ATTRIBUTE else null // attribute
        Kind.FUNCTION -> when (field) {
            7 -> Kind.NODE // node
            11 -> Kind.ATTRIBUTE // attribute_proto
            else -> null
        }
        Kind.ATTRIBUTE -> when (field) {
            5, 10 -> Kind.TENSOR // t, tensors
            6, 11 -> Kind.GRAPH // g, graphs
            22, 23 -> Kind.SPARSE_TENSOR // sparse_tensor, sparse_tensors
            else -> null
        }
        Kind.SPARSE_TENSOR -> when (field) {
            1, 2 -> Kind.TENSOR // values, indices
            else -> null
        }
        Kind.TENSOR -> null
    }

    private class Scan(private val buf: ByteBuffer) {
        var nodes = 0
        var tensors = 0
        var graphs = 0

        private fun malformed(): Nothing = throw UnsafeModel("The file is not a well-formed ONNX model.")

        /**
         * A varint of at most [maxBytes] bytes. Protobuf reads a value in at most 10 bytes and
         * a tag or a length in at most 5, so a longer one is refused, as is a 10th byte with
         * bits past bit 63.
         */
        private fun varint(at: IntArray, end: Int, maxBytes: Int = 10): Long {
            var result = 0L
            var shift = 0
            var count = 0
            while (true) {
                if (at[0] >= end || count >= maxBytes) malformed()
                val b = buf.get(at[0]++).toInt() and 0xFF
                count++
                if (shift == 63 && b > 1) malformed()
                result = result or ((b and 0x7F).toLong() shl shift)
                if (b and 0x80 == 0) return result
                shift += 7
            }
        }

        /**
         * A field tag. Protobuf (and so ONNX Runtime) keeps only the low 32 bits of a tag, so a
         * 5-byte tag padded with bits above bit 31 names one field for ONNX Runtime and another
         * for a 64-bit reader; a model could hide its graph from this scan that way. A tag above
         * 0xFFFFFFFF, longer than 5 bytes, or with a field number outside 1 to 2^29-1 is refused.
         */
        private fun tag(at: IntArray, end: Int): Long {
            val tag = varint(at, end, maxBytes = 5)
            if (tag > 0xFFFF_FFFFL) malformed()
            val field = tag ushr 3
            if (field < 1 || field > MAX_FIELD) malformed()
            return tag
        }

        /**
         * A length-delimited field's length. Protobuf reads it in at most 5 bytes and below
         * 2^31; it must also fit in the bytes left before [end].
         */
        private fun length(at: IntArray, end: Int): Int {
            val len = varint(at, end, maxBytes = 5)
            if (len < 0 || len > Int.MAX_VALUE || len > end - at[0]) malformed()
            return len.toInt()
        }

        fun message(kind: Kind, start: Int, end: Int, depth: Int) {
            if (depth > MAX_DEPTH) throw UnsafeModel("The model nests its graphs too deeply.")
            when (kind) {
                Kind.GRAPH -> graphs++
                Kind.NODE -> if (++nodes > MAX_NODES) throw UnsafeModel("The model has more than $MAX_NODES nodes.")
                Kind.TENSOR -> if (++tensors > MAX_TENSORS) throw UnsafeModel("The model has more than $MAX_TENSORS tensors.")
                else -> Unit
            }
            val at = intArrayOf(start)
            while (at[0] < end) {
                val tag = tag(at, end)
                val field = (tag ushr 3).toInt()
                val wire = (tag and 7).toInt()
                when (wire) {
                    0 -> {
                        val v = varint(at, end)
                        // TensorProto.data_location: EXTERNAL is 1.
                        if (kind == Kind.TENSOR && field == 14 && v != 0L) externalData()
                    }
                    1, 5 -> {
                        if (kind == Kind.TENSOR && field == 14) externalData()
                        skip(at, if (wire == 1) 8 else 4, end)
                    }
                    2 -> {
                        val len = length(at, end)
                        val from = at[0]
                        val to = from + len
                        // TensorProto.external_data: any entry names a file outside the model. A
                        // data_location written with the wrong wire type is refused too.
                        if (kind == Kind.TENSOR && (field == 13 || field == 14)) externalData()
                        val sub = child(kind, field)
                        if (sub != null) message(sub, from, to, depth + 1)
                        at[0] = to
                    }
                    else -> malformed() // groups (3, 4) are not used by ONNX; 6 and 7 are not wire types
                }
            }
            if (at[0] != end) malformed()
        }

        private fun skip(at: IntArray, n: Int, end: Int) {
            if (end - at[0] < n) malformed()
            at[0] += n
        }

        private fun externalData(): Nothing =
            throw UnsafeModel("The model keeps tensor data in other files (external data), which the app does not allow.")
    }
}
