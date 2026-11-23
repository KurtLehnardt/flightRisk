package com.flightrisk.app.demo

import android.graphics.Bitmap

/** The demo pictures of [DemoArt] as bitmaps. They live in memory only and are never saved. */
object DemoImages {
    fun target(): Bitmap =
        Bitmap.createBitmap(DemoArt.targetPixels(), DemoArt.TARGET_WIDTH, DemoArt.TARGET_HEIGHT, Bitmap.Config.ARGB_8888)

    fun scene(): Bitmap =
        Bitmap.createBitmap(DemoArt.scenePixels(), DemoArt.SCENE_WIDTH, DemoArt.SCENE_HEIGHT, Bitmap.Config.ARGB_8888)
}
