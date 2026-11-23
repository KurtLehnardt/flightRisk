package com.flightrisk.app.byo

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import com.flightrisk.app.model.OnnxModelLoader
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/**
 * Imports a detector the user chose with the system file picker
 * (`ACTION_OPEN_DOCUMENT`) and checks it before it is used:
 *
 * 1. the file name ends in `.onnx` and the file is at most 100 MB;
 * 2. it is copied into the app's private storage while its SHA-256 is computed
 *    (the copy stops at the size limit);
 * 3. it keeps no tensor data in other files ([com.flightrisk.app.model.OnnxModelScanner],
 *    checked before ONNX Runtime opens it), and has at most 50,000 nodes;
 * 4. ONNX Runtime opens it, its input is `[1, 3, 640, 640]` and its first
 *    output is `[1, 84, N]` or `[1, 300, 6]`;
 * 5. if it names its classes, class 0 is "person";
 * 6. it finds at least one person on the built-in check image, a drawn scene
 *    of person-like figures ([CheckScene], drawn in code, no photograph).
 *
 * Only then does it replace any earlier import. On any failure the copy is
 * deleted. Nothing about the file is logged. Call [import] off the main thread.
 */
class ByoDetectorImporter(private val context: Context) {

    /** What an import did. */
    sealed class Result {
        data class Imported(val record: ByoDetectorStore.Imported, val checkScore: Float) : Result()
        data class Failed(val reason: String) : Result()
    }

    fun import(uri: Uri): Result {
        val store = ByoDetectorStore(context)
        val folder = store.folder
        // A copy left by an import that did not finish (a crash while a hostile model was
        // being checked, or the app being killed) goes first.
        store.deleteStaleImports()
        val temp = store.newTempFile()
        return try {
            val (name, declaredSize) = describe(uri)
            ByoDetectorChecks.checkName(name)
            // A provider may report no size, or 0; the copy below enforces the limit either way.
            ByoDetectorChecks.checkSize(declaredSize?.takeIf { it > 0L })
            if (!folder.isDirectory && !folder.mkdirs()) throw IOException("cannot create the model folder")
            val (size, sha) = copy(uri, temp)
            ByoDetectorChecks.checkSize(size)
            // Refused before ONNX Runtime opens it: external data could make it read other files.
            ByoDetectorChecks.checkFileIsSafe(temp)
            val (layout, score) = OnnxModelLoader.fromFile(temp).use { model -> ByoDetectorChecks.checkModel(model) }
            val target = store.modelFile
            target.delete()
            if (!temp.renameTo(target)) throw IOException("cannot move the model into place")
            val record = ByoDetectorStore.Imported(
                sha256 = sha,
                sizeBytes = size,
                layout = layout,
                attestationVersion = ByoDetectorStore.ATTESTATION_VERSION,
                attestedAtMillis = System.currentTimeMillis(),
            )
            store.save(record)
            Result.Imported(record, score)
        } catch (e: ByoDetectorChecks.Failure) {
            Result.Failed(e.reason)
        } catch (e: IOException) {
            Result.Failed("The file could not be read or copied.")
        } catch (e: OutOfMemoryError) {
            Result.Failed("The model needs more memory than this phone can give it.")
        } catch (e: Exception) {
            // OrtException and IllegalStateException from the runtime: the file is not a usable model.
            Result.Failed("The file is not an ONNX model this app can run.")
        } catch (e: LinkageError) {
            Result.Failed("The model runtime did not load.")
        } finally {
            temp.delete()
        }
    }

    private fun describe(uri: Uri): Pair<String?, Long?> {
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)
            ?.use { c ->
                if (c.moveToFirst()) {
                    val nameIdx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val sizeIdx = c.getColumnIndex(OpenableColumns.SIZE)
                    val name = if (nameIdx >= 0 && !c.isNull(nameIdx)) c.getString(nameIdx) else null
                    val size = if (sizeIdx >= 0 && !c.isNull(sizeIdx)) c.getLong(sizeIdx) else null
                    return name to size
                }
            }
        return null to null
    }

    /** Copies at most [ByoDetectorChecks.MAX_BYTES] + 1 bytes and returns the size and SHA-256. */
    private fun copy(uri: Uri, to: File): Pair<Long, String> {
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        val input = context.contentResolver.openInputStream(uri) ?: throw IOException("no input stream")
        input.use { ins ->
            to.outputStream().use { out ->
                val buf = ByteArray(1 shl 16)
                while (true) {
                    val n = ins.read(buf)
                    if (n < 0) break
                    total += n
                    if (total > ByoDetectorChecks.MAX_BYTES) throw ByoDetectorChecks.Failure("The file is larger than 100 MB.")
                    digest.update(buf, 0, n)
                    out.write(buf, 0, n)
                }
            }
        }
        return total to ByoDetectorChecks.hex(digest.digest())
    }

}
