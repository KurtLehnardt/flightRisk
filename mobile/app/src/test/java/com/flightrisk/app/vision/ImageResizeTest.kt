package com.flightrisk.app.vision

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

/**
 * The two resize filters against the libraries they reproduce. The expected
 * values were computed with the versions `scripts/mobile_models/requirements.lock`
 * pins, OpenCV 5.1 (`cv2.resize`, INTER_LINEAR; opencv-python-headless
 * 5.1.0.95 on Apple Silicon) and Pillow 13.0 (`Image.resize`, BICUBIC), on the
 * same synthetic image.
 */
class ImageResizeTest {

    /** The synthetic test image: R, G and B are simple functions of x and y. */
    private fun image(w: Int, h: Int) = IntArray(w * h) { i ->
        val x = i % w
        val y = i / w
        val r = (x * 37 + y * 11) % 256
        val g = (x * 13 + y * 29 + 50) % 256
        val b = (x * x * 5 + y * 7 + 3) % 256
        (0xFF shl 24) or (r shl 16) or (g shl 8) or b
    }

    private fun rgb(px: IntArray) = px.flatMap { listOf((it shr 16) and 0xFF, (it shr 8) and 0xFF, it and 0xFF) }.toIntArray()

    private fun cv(w: Int, h: Int, nw: Int, nh: Int) = rgb(ImageResize.bilinearCv(image(w, h), w, h, nw, nh))
    private fun pil(w: Int, h: Int, nw: Int, nh: Int) = rgb(ImageResize.bicubicPillow(image(w, h), w, h, nw, nh))

    private fun sha(px: IntArray) = MessageDigest.getInstance("SHA-256")
        .digest(rgb(px).let { v -> ByteArray(v.size) { v[it].toByte() } })
        .joinToString("") { "%02x".format(it) }

    @Test
    fun `bilinear matches OpenCV when shrinking`() {
        assertArrayEquals(
            intArrayOf(6, 56, 5, 52, 72, 15, 98, 88, 40, 144, 104, 80, 191, 121, 136, 141, 137, 208, 27, 153, 136, 73, 169, 142, 120, 186, 229, 166, 202, 140, 212, 218, 130, 34, 206, 199, 49, 191, 92, 95, 11, 66, 141, 27, 118, 187, 43, 26, 20, 92, 14, 66, 108, 24, 112, 124, 49, 158, 140, 89, 205, 157, 145, 155, 173, 181, 41, 189, 109, 87, 205, 151, 134, 222, 154, 180, 202, 149, 226, 158, 139, 48, 34, 208, 63, 31, 101, 109, 47, 75, 155, 63, 127, 201, 79, 35, 34, 128, 22, 80, 144, 32, 126, 160, 57, 172, 176, 97, 219, 193, 153, 169, 209, 129, 55, 225, 57, 101, 241, 159, 148, 98, 22, 194, 78, 157, 140, 34, 147, 42, 50, 216, 77, 67, 109, 123, 83, 83, 169, 99, 135, 215, 115, 43, 48, 164, 31, 94, 180, 41, 140, 196, 66, 186, 212, 106, 205, 229, 162, 43, 161, 138, 69, 37, 66, 115, 53, 168, 162, 38, 31, 208, 54, 166, 94, 70, 156, 44, 86, 225, 91, 103, 118, 137, 119, 92, 183, 135, 144, 229, 151, 52, 61, 201, 40, 107, 217, 50, 153, 213, 75, 199, 217, 115, 214, 10, 171, 36, 26, 147, 82, 42, 75, 128, 58, 177, 175, 75, 40, 221, 91, 175, 107, 107, 165, 57, 123, 206, 104, 140, 127, 150, 156, 101, 196, 172, 153, 214, 188, 61, 75, 225, 49, 121, 157, 59, 167, 73, 84, 213, 29, 124, 144, 46, 180, 50, 62, 156, 96, 78, 84, 142, 94, 186, 189, 111, 49, 199, 127, 148, 85, 143, 138, 71, 159, 19, 118, 176, 136, 164, 192, 110, 210, 208, 162, 32, 224, 70, 89, 101, 57, 135, 33, 67, 181, 49, 92, 227, 65, 132, 18, 82, 188, 64, 98, 164, 110, 114, 92, 156, 130, 194, 203, 147, 57, 153, 163, 96, 39, 179, 86, 85, 195, 27, 132, 212, 144, 178, 228, 118, 224, 144, 170, 46, 100, 78, 103, 53, 66, 149, 69, 76, 195, 85, 101, 241, 101, 141, 32, 118, 197, 78, 134, 173, 124, 150, 101, 170, 166, 203, 217, 183, 66, 167, 199, 105, 53, 215, 95, 99, 231, 36, 146, 220, 153, 192, 40, 127, 238, 36, 179, 60, 40, 87),
            cv(20, 10, 16, 8),
        )
        // The golden frame's size.
        assertEquals("0b34e64ca1d4f978630da4c723c26cf5dbacdfa339be5bb3973cc28f818a6c71", sha(ImageResize.bilinearCv(image(800, 480), 800, 480, 640, 384)))
    }

    @Test
    fun `bilinear matches OpenCV's 2 x 2 mean when both sides halve, as for 1280 x 720 camera frames`() {
        assertArrayEquals(
            intArrayOf(24, 71, 9, 98, 97, 39, 172, 123, 109, 118, 149, 219, 64, 175, 113, 138, 201, 175, 212, 227, 149, 30, 125, 163, 46, 129, 23, 120, 155, 53, 194, 181, 123, 140, 207, 105, 86, 233, 127, 160, 131, 61, 170, 29, 163, 52, 55, 177, 68, 187, 37, 142, 213, 67, 216, 175, 137, 34, 73, 119, 108, 35, 141, 182, 61, 75, 128, 87, 177, 74, 113, 191, 90, 181, 51, 164, 79, 81, 174, 41, 151, 56, 67, 133, 130, 93, 155, 204, 119, 89, 86, 145, 127, 96, 171, 77, 112, 47, 65, 186, 73, 95, 132, 99, 165, 78, 125, 147, 152, 151, 169, 226, 177, 103, 44, 203, 77, 118, 229, 91, 134, 105, 79, 208, 131, 109, 26, 157, 179, 100, 183, 97, 174, 209, 183, 120, 171, 117, 66, 133, 91, 140, 31, 105),
            cv(16, 12, 8, 6),
        )
        assertEquals("2a14f5fae7c4a41d2a9e95647496ba70f6cb7e7c417dd13b037a5002c7f66ba2", sha(ImageResize.bilinearCv(image(1280, 720), 1280, 720, 640, 360)))
    }

    @Test
    fun `bilinear matches OpenCV when enlarging`() {
        assertArrayEquals(
            intArrayOf(0, 50, 3, 17, 56, 5, 40, 64, 9, 64, 72, 19, 87, 81, 32, 111, 89, 48, 135, 97, 70, 158, 106, 95, 182, 114, 124, 205, 122, 158, 222, 128, 183, 4, 60, 5, 21, 66, 7, 44, 74, 11, 68, 82, 21, 91, 91, 34, 115, 99, 50, 139, 107, 72, 162, 116, 97, 186, 124, 126, 209, 132, 160, 226, 138, 185, 10, 76, 9, 27, 82, 11, 50, 90, 15, 74, 98, 25, 97, 107, 38, 121, 115, 54, 145, 123, 76, 168, 132, 101, 192, 140, 130, 215, 148, 164, 232, 154, 189, 16, 92, 13, 33, 98, 15, 56, 106, 19, 80, 114, 29, 103, 123, 42, 127, 131, 58, 151, 139, 80, 174, 148, 105, 198, 156, 134, 221, 164, 168, 238, 170, 193, 22, 108, 17, 39, 114, 19, 62, 122, 23, 86, 130, 33, 109, 139, 46, 133, 147, 62, 157, 155, 84, 180, 164, 109, 204, 172, 138, 227, 180, 172, 244, 186, 197, 28, 124, 21, 45, 130, 23, 68, 138, 27, 92, 146, 37, 115, 155, 50, 139, 163, 66, 163, 171, 88, 186, 180, 113, 210, 188, 142, 233, 196, 176, 250, 202, 201, 34, 140, 25, 51, 146, 27, 74, 154, 31, 98, 162, 41, 121, 171, 54, 145, 179, 70, 169, 187, 92, 192, 196, 117, 216, 204, 146, 224, 212, 180, 228, 218, 205, 40, 156, 29, 57, 162, 31, 80, 170, 35, 104, 178, 45, 127, 187, 58, 151, 195, 74, 175, 203, 96, 198, 212, 121, 222, 220, 150, 153, 228, 184, 92, 234, 209, 44, 166, 31, 61, 172, 33, 84, 180, 37, 108, 188, 47, 131, 197, 60, 155, 205, 76, 179, 213, 98, 202, 222, 123, 226, 230, 152, 110, 238, 186, 10, 244, 211),
            cv(7, 5, 11, 9),
        )
    }

    @Test
    fun `bicubic matches Pillow when shrinking, enlarging and both`() {
        assertArrayEquals(
            intArrayOf(19, 65, 6, 81, 87, 27, 149, 111, 82, 211, 133, 162, 37, 113, 18, 99, 135, 39, 167, 159, 94, 234, 181, 174, 55, 161, 30, 117, 183, 51, 191, 207, 106, 165, 229, 186),
            pil(7, 5, 4, 3),
        )
        assertArrayEquals(
            intArrayOf(26, 57, 6, 110, 87, 50, 194, 117, 143, 32, 73, 9, 116, 103, 53, 200, 133, 146, 41, 96, 15, 125, 126, 59, 209, 156, 152, 49, 117, 20, 133, 147, 64, 217, 177, 157, 57, 138, 25, 140, 168, 69, 232, 198, 162, 66, 161, 31, 153, 191, 75, 190, 221, 168, 72, 177, 34, 164, 207, 78, 136, 237, 171),
            pil(7, 5, 3, 7),
        )
        assertArrayEquals(
            intArrayOf(6, 74, 9, 17, 78, 10, 40, 86, 13, 60, 93, 19, 80, 100, 27, 100, 107, 39, 120, 114, 54, 140, 121, 72, 160, 128, 92, 179, 135, 116, 201, 142, 143, 229, 150, 177, 243, 154, 194, 32, 140, 25, 43, 144, 26, 66, 152, 29, 86, 159, 35, 106, 166, 43, 126, 173, 55, 146, 180, 70, 166, 187, 88, 186, 194, 108, 212, 201, 132, 216, 208, 159, 182, 216, 193, 164, 220, 210),
            // The height shrinks by more than twice as many pixels as the width grows: columns first.
            pil(7, 5, 13, 2),
        )
    }

    @Test
    fun `bicubic matches Pillow on the enlargement the embedder makes of a 19 x 49 crop`() {
        // 19 x 49 is the golden crop's size; Pillow gives 224 x 577 pixels whose RGB bytes hash to this.
        val out = ImageResize.bicubicPillow(image(19, 49), 19, 49, 224, 577)
        assertEquals(48999106L, rgb(out).sumOf { it.toLong() })
        assertEquals("9442d79203d0808c11407747e6cb70273ef19dc9cb9ae15bb1cc1918ee0f1025", sha(out))
    }

    @Test
    fun `bicubic matches Pillow on a crop larger than the embedder's input, both pass orders`() {
        // 300 x 601 to 224 x 448: the height shrinks by 153, more than twice the width's 76, so
        // Pillow 13 resizes the columns first; 601 x 300 to 448 x 224 goes rows first.
        assertEquals("878f8f7f7aa3d6cf81420ab640b25d98017ec020057720f977ca7b60a438c225", sha(ImageResize.bicubicPillow(image(300, 601), 300, 601, 224, 448)))
        assertEquals("c9c35142eec71536f2102299e1b57c1182dcb21de11e528368a612efb1b569ac", sha(ImageResize.bicubicPillow(image(601, 300), 601, 300, 448, 224)))
    }

    @Test
    fun `a constant image stays constant and an unchanged size is a copy`() {
        val grey = IntArray(30 * 20) { (0xFF shl 24) or 0x808080 }
        for (out in listOf(
            ImageResize.bilinearCv(grey, 30, 20, 17, 11),
            ImageResize.bilinearCv(grey, 30, 20, 47, 33),
            ImageResize.bicubicPillow(grey, 30, 20, 17, 11),
            ImageResize.bicubicPillow(grey, 30, 20, 47, 33),
        )) {
            assertEquals(setOf((0xFF shl 24) or 0x808080), out.toSet())
        }
        val img = image(9, 7)
        assertArrayEquals(img, ImageResize.bilinearCv(img, 9, 7, 9, 7))
        assertArrayEquals(img, ImageResize.bicubicPillow(img, 9, 7, 9, 7))
    }
}
