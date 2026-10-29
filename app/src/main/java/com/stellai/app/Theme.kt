package com.stellai.app

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.random.Random

/** StellAI palette: deep space, with cyan and violet glows and a gold accent. */
object Space {
    val Void = Color(0xFF04060D)
    val Top = Color(0xFF070B1A)
    val Bottom = Color(0xFF0B0620)
    val Panel = Color(0xFF0C1326)
    val PanelHigh = Color(0xFF141D38)
    val Line = Color(0xFF26335C)
    val Cyan = Color(0xFF5CE1E6)
    val Violet = Color(0xFF9B7BFF)
    val Gold = Color(0xFFFFD166)
    val Text = Color(0xFFE6ECFF)
    val Muted = Color(0xFF8A96BF)
    val Red = Color(0xFFFF6B7A)
}

private val StellarColors = darkColorScheme(
    primary = Space.Cyan,
    onPrimary = Color(0xFF00262B),
    primaryContainer = Color(0xFF0F3A44),
    onPrimaryContainer = Color(0xFFC4F7F9),
    secondary = Space.Violet,
    onSecondary = Color(0xFF1B0B45),
    secondaryContainer = Color(0xFF2A1F55),
    onSecondaryContainer = Color(0xFFE3DAFF),
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
    MaterialTheme(colorScheme = StellarColors, typography = Typography(), content = content)
}

private class Star(val x: Float, val y: Float, val radius: Float, val alpha: Float, val tint: Color)

/** A still starfield with two faint nebula glows. Drawn once; no animation, so it's battery-friendly. */
@Composable
fun Starfield(modifier: Modifier = Modifier) {
    val stars = remember {
        val r = Random(42)
        List(160) {
            val tint = when (r.nextInt(10)) {
                0 -> Space.Cyan
                1 -> Space.Violet
                else -> Color.White
            }
            Star(r.nextFloat(), r.nextFloat(), r.nextFloat() * 1.3f + 0.3f, r.nextFloat() * 0.65f + 0.15f, tint)
        }
    }
    Canvas(modifier.fillMaxSize()) {
        drawRect(Brush.verticalGradient(listOf(Space.Top, Space.Void, Space.Bottom)))

        val glow1 = Offset(size.width * 0.9f, size.height * 0.12f)
        drawCircle(
            Brush.radialGradient(listOf(Space.Cyan.copy(alpha = 0.13f), Color.Transparent), glow1, size.width * 0.75f),
            radius = size.width * 0.75f, center = glow1,
        )
        val glow2 = Offset(size.width * 0.05f, size.height * 0.85f)
        drawCircle(
            Brush.radialGradient(listOf(Space.Violet.copy(alpha = 0.16f), Color.Transparent), glow2, size.width * 0.85f),
            radius = size.width * 0.85f, center = glow2,
        )

        for (s in stars) {
            drawCircle(
                color = s.tint.copy(alpha = s.alpha),
                radius = s.radius.dp.toPx(),
                center = Offset(s.x * size.width, s.y * size.height),
            )
        }
    }
}
