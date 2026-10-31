package com.flightrisk.app.ui.ground

import androidx.compose.ui.graphics.Color
import kotlin.math.max
import kotlin.math.min

/**
 * The height budget of the Ground screen. The pinned top bar must never push
 * the drone list (and every per-drone Land button in it) off the screen, so
 * the scrollable text area of the bar is limited, and the list always keeps a
 * guaranteed minimum.
 */
object GroundLayout {
    /** The share of the screen the bar's text area may take. */
    const val FEEDBACK_SHARE = 0.35f

    /** The part of the bar that never scrolls: the pill row, the LAND ALL button, the retry and dismiss buttons and the padding. */
    const val FIXED_BAR_DP = 140f

    /** The least height the drone list keeps, enough for one card with its Land button. */
    const val MIN_LIST_DP = 180f

    /** The least height the text area keeps, so a short message is still readable. */
    const val MIN_FEEDBACK_DP = 48f

    /**
     * The largest height the bar's scrolling text area may have on a screen
     * [availableDp] high: 35% of the screen, and never so much that the list
     * would drop below [MIN_LIST_DP]. On a screen too small for both the text
     * area keeps [MIN_FEEDBACK_DP].
     */
    fun feedbackMaxHeightDp(availableDp: Float): Float {
        val share = availableDp * FEEDBACK_SHARE
        val leaveForList = availableDp - FIXED_BAR_DP - MIN_LIST_DP
        return max(MIN_FEEDBACK_DP, min(share, leaveForList))
    }

    /** The height the list keeps when the text area is full. */
    fun listHeightDp(availableDp: Float): Float = availableDp - FIXED_BAR_DP - feedbackMaxHeightDp(availableDp)
}

/**
 * Chip and text colours of the Ground screen, each pair at least 4.5:1
 * (WCAG AA). The error container pairs mirror the theme's `errorContainer`
 * and `onErrorContainer` in both modes; a test checks every pair here.
 */
object GroundColors {
    val ChipText = Color.White
    val ChipGreen = Color(0xFF166534)
    val ChipOrange = Color(0xFF9A3412)
    val ChipGrey = Color(0xFF595959)
    val ChipRed = Color(0xFFB91C1C)
    val ChipBlue = Color(0xFF1D4ED8)

    val LightErrorContainer = Color(0xFFFFDAD6)
    val LightOnErrorContainer = Color(0xFF410002)
    val DarkErrorContainer = Color(0xFF93000A)
    val DarkOnErrorContainer = Color(0xFFFFDAD6)

    /** Every (foreground, background) pair the screen draws text with. */
    val pairs: List<Pair<Color, Color>> = listOf(
        ChipText to ChipGreen, ChipText to ChipOrange, ChipText to ChipGrey, ChipText to ChipRed, ChipText to ChipBlue,
        LightOnErrorContainer to LightErrorContainer, DarkOnErrorContainer to DarkErrorContainer,
    )

    /** WCAG relative luminance contrast ratio of [a] on [b]. */
    fun contrast(a: Color, b: Color): Double {
        val la = luminance(a)
        val lb = luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
    }

    private fun luminance(c: Color): Double {
        fun lin(v: Float): Double = if (v <= 0.03928f) v / 12.92 else Math.pow((v + 0.055) / 1.055, 2.4)
        return 0.2126 * lin(c.red) + 0.7152 * lin(c.green) + 0.0722 * lin(c.blue)
    }
}
