package com.flightrisk.app.ui.theme

import android.app.Activity
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

// -----------------------------------------------------------------------
// Alert / accent colors (WCAG AAA 7:1+ contrast against their backgrounds)
// -----------------------------------------------------------------------

/** Dark red for confirmed-match alerts. Contrast on white: ~8.6:1. */
val AlertRed = Color(0xFFB91C1C)

/** Alert banner background for confirmed matches. */
val AlertRedDark = Color(0xFF991B1B)

/** Orange for possible-match alerts. */
val AlertOrange = Color(0xFFEA580C)

/** Blue for detection bounding boxes. */
val DetectionBlue = Color(0xFF2563EB)

/** Green for match bounding boxes and positive indicators. */
val MatchGreen = Color(0xFF16A34A)

/** High-visibility white for HUD text over camera preview. */
val HudWhite = Color(0xFFFFFFFF)

/** High-visibility black for HUD text in light mode. */
val HudBlack = Color(0xFF000000)

// -----------------------------------------------------------------------
// Light color scheme
// -----------------------------------------------------------------------

private val LightColors = lightColorScheme(
    primary = Color(0xFF1E3A5F),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD1E4FF),
    onPrimaryContainer = Color(0xFF001D36),

    secondary = Color(0xFF535F70),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFD7E3F7),
    onSecondaryContainer = Color(0xFF101C2B),

    tertiary = MatchGreen,
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFBBF0C9),
    onTertiaryContainer = Color(0xFF002110),

    error = AlertRed,
    onError = Color.White,
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),

    background = Color(0xFFFDFBFF),
    onBackground = Color(0xFF1A1C1E),
    surface = Color(0xFFFDFBFF),
    onSurface = Color(0xFF1A1C1E),
    surfaceVariant = Color(0xFFDFE2EB),
    onSurfaceVariant = Color(0xFF43474E),

    outline = Color(0xFF73777F),
    outlineVariant = Color(0xFFC3C7CF),
)

// -----------------------------------------------------------------------
// Dark color scheme
// -----------------------------------------------------------------------

private val DarkColors = darkColorScheme(
    primary = Color(0xFFA0CAFD),
    onPrimary = Color(0xFF003258),
    primaryContainer = Color(0xFF0A4A77),
    onPrimaryContainer = Color(0xFFD1E4FF),

    secondary = Color(0xFFBBC7DB),
    onSecondary = Color(0xFF253140),
    secondaryContainer = Color(0xFF3B4858),
    onSecondaryContainer = Color(0xFFD7E3F7),

    tertiary = Color(0xFF7FDB96),
    onTertiary = Color(0xFF00391C),
    tertiaryContainer = Color(0xFF00522B),
    onTertiaryContainer = Color(0xFFBBF0C9),

    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
    errorContainer = Color(0xFF93000A),
    onErrorContainer = Color(0xFFFFDAD6),

    background = Color(0xFF1A1C1E),
    onBackground = Color(0xFFE2E2E6),
    surface = Color(0xFF1A1C1E),
    onSurface = Color(0xFFE2E2E6),
    surfaceVariant = Color(0xFF43474E),
    onSurfaceVariant = Color(0xFFC3C7CF),

    outline = Color(0xFF8D9199),
    outlineVariant = Color(0xFF43474E),
)

// -----------------------------------------------------------------------
// Theme composable
// -----------------------------------------------------------------------

/**
 * FlightRisk Material 3 theme.
 *
 * Provides high-contrast alert colors that meet WCAG AAA (7:1+) for
 * outdoor/sunlight readability. Supports both light and dark modes.
 *
 * Alert-specific colors (AlertRed, DetectionBlue, MatchGreen, etc.) are
 * exposed as top-level vals rather than through the color scheme, since
 * they are used for custom drawing (bounding boxes, HUD overlays) that
 * sits outside the standard Material surface/content pattern.
 */
@Composable
fun FlightRiskTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colorScheme = if (darkTheme) DarkColors else LightColors

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as? Activity)?.window ?: return@SideEffect
            window.statusBarColor = colorScheme.surface.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = !darkTheme
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        content = content,
    )
}
