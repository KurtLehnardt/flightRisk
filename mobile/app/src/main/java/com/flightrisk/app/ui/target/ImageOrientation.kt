package com.flightrisk.app.ui.target

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import androidx.exifinterface.media.ExifInterface

/**
 * Decoding of a photo on Android 8.0 and 8.1 (API 26 and 27), where neither `ImageDecoder`
 * nor `MediaStore.Images.Media.getBitmap` is usable: the latter loads the full-size bitmap
 * and ignores the EXIF orientation, so camera photos came out sideways. The helpers here
 * are plain functions so the rotation and the sample size can be unit tested.
 */
internal object ImageOrientation {

    /** The largest edge the target picker keeps, in pixels. */
    const val MAX_EDGE = 1920

    /** Clockwise degrees to rotate a decoded bitmap so it is upright, for an EXIF orientation value. */
    fun rotationDegrees(exifOrientation: Int): Int = when (exifOrientation) {
        ExifInterface.ORIENTATION_ROTATE_90, ExifInterface.ORIENTATION_TRANSPOSE -> 90
        ExifInterface.ORIENTATION_ROTATE_180, ExifInterface.ORIENTATION_FLIP_VERTICAL -> 180
        ExifInterface.ORIENTATION_ROTATE_270, ExifInterface.ORIENTATION_TRANSVERSE -> 270
        else -> 0
    }

    /** Whether the EXIF orientation also mirrors the image (a front-camera capture). */
    fun isMirrored(exifOrientation: Int): Boolean = when (exifOrientation) {
        ExifInterface.ORIENTATION_FLIP_HORIZONTAL,
        ExifInterface.ORIENTATION_TRANSPOSE,
        ExifInterface.ORIENTATION_FLIP_VERTICAL,
        ExifInterface.ORIENTATION_TRANSVERSE,
        -> true
        else -> false
    }

    /**
     * The largest power-of-two `inSampleSize` that keeps the long edge at [maxEdge] or more, so a
     * large photo is decoded at reduced size and the full bitmap is never held in memory.
     */
    fun sampleSize(width: Int, height: Int, maxEdge: Int = MAX_EDGE): Int {
        if (width <= 0 || height <= 0 || maxEdge <= 0) return 1
        var sample = 1
        var longEdge = maxOf(width, height)
        while (longEdge / 2 >= maxEdge) {
            sample *= 2
            longEdge /= 2
        }
        return sample
    }

    /** The matrix that makes a bitmap with [exifOrientation] upright. */
    fun matrixFor(exifOrientation: Int): Matrix = Matrix().apply {
        // Rotate first, then mirror: this is the order the EXIF specification defines for the
        // transpose and transverse values.
        val degrees = rotationDegrees(exifOrientation)
        if (degrees != 0) postRotate(degrees.toFloat())
        if (isMirrored(exifOrientation)) postScale(-1f, 1f)
    }

    /**
     * Decodes [uri] reduced to about [MAX_EDGE] and rotated upright by its EXIF orientation.
     * Returns null when the photo cannot be read.
     */
    fun decode(context: Context, uri: Uri): Bitmap? {
        val resolver = context.contentResolver
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) } ?: return null
        val options = BitmapFactory.Options().apply { inSampleSize = sampleSize(bounds.outWidth, bounds.outHeight) }
        val decoded = resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, options) } ?: return null
        val orientation = try {
            resolver.openInputStream(uri)?.use {
                ExifInterface(it).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
            } ?: ExifInterface.ORIENTATION_NORMAL
        } catch (_: Exception) {
            ExifInterface.ORIENTATION_NORMAL
        }
        if (orientation == ExifInterface.ORIENTATION_NORMAL || orientation == ExifInterface.ORIENTATION_UNDEFINED) return decoded
        val rotated = Bitmap.createBitmap(decoded, 0, 0, decoded.width, decoded.height, matrixFor(orientation), true)
        if (rotated !== decoded) decoded.recycle()
        return rotated
    }
}
