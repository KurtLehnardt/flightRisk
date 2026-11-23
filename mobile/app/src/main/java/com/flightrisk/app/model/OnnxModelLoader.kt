package com.flightrisk.app.model

import android.content.Context
import com.flightrisk.app.vision.OnnxFloatModel
import java.io.File
import java.io.FileInputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.channels.FileChannel

/**
 * Opens the app's ONNX models.
 *
 * The models `scripts/mobile_models/make_models.py install` copies into the
 * assets are large (the embedder is about 90 MB), so they are not read into
 * the Java heap: `.onnx` assets are stored uncompressed in the APK
 * (`androidResources.noCompress` in `app/build.gradle.kts`) and memory-mapped.
 */
object OnnxModelLoader {

    /** Opens the asset [assetFileName]. Throws [IOException] when it is missing, [IllegalStateException] when the runtime fails. */
    fun fromAsset(context: Context, assetFileName: String): OnnxFloatModel {
        val buffer = mapAsset(context, assetFileName)
        return guarded { OnnxFloatModel.open(buffer) }
    }

    /** Opens a model file. A file the user supplied must pass [OnnxModelScanner] first: use [fromImportedFile]. */
    fun fromFile(file: File): OnnxFloatModel = guarded { OnnxFloatModel.open(file.absolutePath) }

    /**
     * Opens a model the user imported, after [OnnxModelScanner] has checked it
     * again (it was checked on import; this keeps a file changed since then
     * from reaching ONNX Runtime). Throws [OnnxModelScanner.UnsafeModel].
     */
    fun fromImportedFile(file: File): OnnxFloatModel {
        OnnxModelScanner.scan(file)
        return fromFile(file)
    }

    /** True when the asset exists. */
    fun hasAsset(context: Context, assetFileName: String): Boolean =
        try {
            context.assets.open(assetFileName).close()
            true
        } catch (e: IOException) {
            false
        }

    private fun mapAsset(context: Context, name: String): ByteBuffer =
        try {
            context.assets.openFd(name).use { fd ->
                FileInputStream(fd.fileDescriptor).channel.use { channel ->
                    channel.map(FileChannel.MapMode.READ_ONLY, fd.startOffset, fd.declaredLength)
                }
            }
        } catch (e: IOException) {
            // A compressed asset cannot be mapped (openFd fails); copy it into native memory instead.
            try {
                context.assets.open(name).use { input ->
                    val bytes = input.readBytes()
                    ByteBuffer.allocateDirect(bytes.size).put(bytes).also { it.flip() }
                }
            } catch (oom: OutOfMemoryError) {
                throw IOException("not enough memory to read $name", oom)
            }
        }

    private fun <T> guarded(block: () -> T): T =
        try {
            block()
        } catch (e: LinkageError) {
            // UnsatisfiedLinkError (the native library did not load) or
            // ExceptionInInitializerError (its static initialiser failed) are Errors, so
            // callers that catch Exception would crash. Report them as an ordinary
            // failure so the caller degrades to "models not loaded".
            throw IllegalStateException("The ONNX Runtime native library did not load (${e.javaClass.simpleName})", e)
        }
}
