package com.stellai.app

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.random.Random

/** One selectable accent: the main glow colour and a second colour for gradients and nebulae. */
data class Accent(val id: String, val label: String, val main: Color, val glow: Color)

val ACCENTS = listOf(
    Accent("cyan", "Cyan", Color(0xFF5CE1E6), Color(0xFF9B7BFF)),
    Accent("blue", "Blue", Color(0xFF5AA9FF), Color(0xFF7B8CFF)),
    Accent("red", "Red", Color(0xFFFF5C6C), Color(0xFFFF9A5C)),
    Accent("green", "Green", Color(0xFF4DE8A0), Color(0xFF5CC8E6)),
    Accent("purple", "Purple", Color(0xFFA77BFF), Color(0xFFE07BFF)),
    Accent("pink", "Pink", Color(0xFFFF7AC6), Color(0xFFB57BFF)),
    Accent("orange", "Orange", Color(0xFFFFA552), Color(0xFFFF6B6B)),
    Accent("yellow", "Yellow", Color(0xFFFFD84D), Color(0xFFFFA552)),
)

fun accentFor(id: String): Accent = ACCENTS.firstOrNull { it.id == id } ?: ACCENTS[0]

/**
 * StellAI palette: deep space plus the user's accent colour.
 * Accent and Glow follow the setting live (they read Compose state), so the whole UI recolours instantly.
 */
object Space {
    val Void = Color(0xFF04060D)
    val Top = Color(0xFF070B1A)
    val Bottom = Color(0xFF0B0620)
    val Panel = Color(0xFF0C1326)
    val PanelHigh = Color(0xFF141D38)
    val Line = Color(0xFF26335C)
    val Gold = Color(0xFFFFD166)
    val Text = Color(0xFFE6ECFF)
    val Muted = Color(0xFF8A96BF)
    val Red = Color(0xFFFF6B7A)
    val OnAccent = Color(0xFF0A0A14)

    val Accent: Color get() = accentFor(App.instance.settings.accentColor).main
    val Glow: Color get() = accentFor(App.instance.settings.accentColor).glow
}

private fun stellarColors(accent: Accent): ColorScheme = darkColorScheme(
    primary = accent.main,
    onPrimary = Space.OnAccent,
    primaryContainer = lerp(Space.Panel, accent.main, 0.3f),
    onPrimaryContainer = lerp(Color.White, accent.main, 0.25f),
    secondary = accent.glow,
    onSecondary = Space.OnAccent,
    secondaryContainer = lerp(Space.Panel, accent.glow, 0.3f),
    onSecondaryContainer = lerp(Color.White, accent.glow, 0.25f),
    tertiary = Space.Gold,
    onTertiary = Color(0xFF3A2A00),
    background = Space.Void,
    onBackground = Space.Text,
    surface = Space.Panel,
    onSurface = Space.Text,
    surfaceVariant = Space.PanelHigh,
    onSurfaceVariant = Space.Muted,
    surfaceContainerLowest = Space.Void,
    surfaceContainerLow = Color(0xFF0A1022),
    surfaceContainer = Space.Panel,
    surfaceContainerHigh = Color(0xFF111A33),
    surfaceContainerHighest = Space.PanelHigh,
    outline = Space.Line,
    outlineVariant = Color(0xFF1B2547),
    error = Space.Red,
    onError = Color(0xFF3B0010),
    errorContainer = Color(0xFF3A1020),
    onErrorContainer = Color(0xFFFFC2C9),
)

/** Monospace "console" style for labels and status lines. */
val HudLabel = TextStyle(
    fontFamily = FontFamily.Monospace,
    fontWeight = FontWeight.Medium,
    fontSize = 12.sp,
    letterSpacing = 1.5.sp,
)

@Composable
fun StellarTheme(content: @Composable () -> Unit) {
    val accent = accentFor(App.instance.settings.accentColor)
    MaterialTheme(colorScheme = stellarColors(accent), typography = Typography(), content = content)
}

private class Star(val x: Float, val y: Float, val radius: Float, val alpha: Float, val kind: Int)

/** A still starfield with two faint nebula glows in the accent colours. No animation, so it's battery-friendly. */
@Composable
fun Starfield(modifier: Modifier = Modifier) {
    val stars = remember {
        val r = Random(42)
        List(160) { Star(r.nextFloat(), r.nextFloat(), r.nextFloat() * 1.3f + 0.3f, r.nextFloat() * 0.65f + 0.15f, r.nextInt(10)) }
    }
    Canvas(modifier.fillMaxSize()) {
        val accent = Space.Accent
        val glow = Space.Glow
        drawRect(Brush.verticalGradient(listOf(Space.Top, Space.Void, Space.Bottom)))

        val glow1 = Offset(size.width * 0.9f, size.height * 0.12f)
        drawCircle(
            Brush.radialGradient(listOf(accent.copy(alpha = 0.13f), Color.Transparent), glow1, size.width * 0.75f),
            radius = size.width * 0.75f, center = glow1,
        )
        val glow2 = Offset(size.width * 0.05f, size.height * 0.85f)
        drawCircle(
            Brush.radialGradient(listOf(glow.copy(alpha = 0.16f), Color.Transparent), glow2, size.width * 0.85f),
            radius = size.width * 0.85f, center = glow2,
        )

        for (s in stars) {
            val tint = when (s.kind) {
                0 -> accent
                1 -> glow
                else -> Color.White
            }
            drawCircle(
                color = tint.copy(alpha = s.alpha),
                radius = s.radius.dp.toPx(),
                center = Offset(s.x * size.width, s.y * size.height),
            )
        }
    }
}
