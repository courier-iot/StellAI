package com.stellai.app

import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Alarm
import androidx.compose.material.icons.outlined.Bolt
import androidx.compose.material.icons.outlined.Build
import androidx.compose.material.icons.outlined.Cancel
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.material.icons.outlined.Code
import androidx.compose.material.icons.outlined.DarkMode
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.EditNote
import androidx.compose.material.icons.outlined.FavoriteBorder
import androidx.compose.material.icons.outlined.HistoryEdu
import androidx.compose.material.icons.outlined.Image
import androidx.compose.material.icons.outlined.Keyboard
import androidx.compose.material.icons.outlined.Lightbulb
import androidx.compose.material.icons.outlined.Map
import androidx.compose.material.icons.outlined.MusicNote
import androidx.compose.material.icons.outlined.PictureAsPdf
import androidx.compose.material.icons.outlined.Restaurant
import androidx.compose.material.icons.outlined.School
import androidx.compose.material.icons.outlined.Science
import androidx.compose.material.icons.outlined.ShowChart
import androidx.compose.material.icons.outlined.SportsEsports
import androidx.compose.material.icons.outlined.StarOutline
import androidx.compose.material.icons.outlined.WarningAmber
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/** Line-drawn icons used instead of emoji throughout the app. */
object LineIcons {
    /** Stella's four-point star, drawn as an outline (the app's mark). */
    val Sparkle: ImageVector by lazy {
        ImageVector.Builder("Sparkle", 24.dp, 24.dp, 24f, 24f).apply {
            path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.6f,
                strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round) {
                moveTo(12f, 2.5f)
                curveTo(12.9f, 8.2f, 15.8f, 11.1f, 21.5f, 12f)
                curveTo(15.8f, 12.9f, 12.9f, 15.8f, 12f, 21.5f)
                curveTo(11.1f, 15.8f, 8.2f, 12.9f, 2.5f, 12f)
                curveTo(8.2f, 11.1f, 11.1f, 8.2f, 12f, 2.5f)
                close()
            }
            path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.4f, strokeLineCap = StrokeCap.Round) {
                moveTo(19f, 3f); lineTo(19f, 6f); moveTo(17.5f, 4.5f); lineTo(20.5f, 4.5f)
            }
        }.build()
    }

    /**
     * A mode's icon. Modes store a symbol (shared with the desktop chat), which is drawn here as a line icon.
     */
    fun forMode(symbol: String): ImageVector = when (symbol.trim()) {
        "⌨" -> Icons.Outlined.Keyboard
        "✒", "✍", "✏" -> Icons.Outlined.EditNote
        "⚡" -> Icons.Outlined.Bolt
        "🎓" -> Icons.Outlined.School
        "💡" -> Icons.Outlined.Lightbulb
        "📜" -> Icons.Outlined.HistoryEdu
        "🎮" -> Icons.Outlined.SportsEsports
        "🍳" -> Icons.Outlined.Restaurant
        "🧪" -> Icons.Outlined.Science
        "🎵", "🎶" -> Icons.Outlined.MusicNote
        "📈" -> Icons.Outlined.ShowChart
        "🗺" -> Icons.Outlined.Map
        "🛠", "🔧" -> Icons.Outlined.Build
        "❤", "♥" -> Icons.Outlined.FavoriteBorder
        "🌙" -> Icons.Outlined.DarkMode
        "★", "⭐" -> Icons.Outlined.StarOutline
        "💻" -> Icons.Outlined.Code
        else -> Sparkle
    }

    /** Icons offered when you make your own mode (stored as their symbol). */
    val MODE_CHOICES = listOf("★", "⌨", "✒", "⚡", "🎓", "💡", "📜", "🎮", "🍳", "🧪", "🎵", "📈", "🗺", "🛠", "❤", "🌙")

    fun forFile(kind: String): ImageVector = when (kind) {
        "pdf" -> Icons.Outlined.PictureAsPdf
        "image" -> Icons.Outlined.Image
        "code" -> Icons.Outlined.Code
        else -> Icons.Outlined.Description
    }

    val Reminder get() = Icons.Outlined.Alarm
}

/** A small line icon followed by text, in one colour. */
@Composable
fun IconText(icon: ImageVector, text: String, modifier: Modifier = Modifier, color: Color = LocalContentColor.current,
             style: TextStyle = TextStyle.Default, size: Dp = 16.dp, maxLines: Int = Int.MAX_VALUE) {
    Row(modifier, verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, contentDescription = null, tint = color, modifier = Modifier.size(size))
        Spacer(Modifier.width(6.dp))
        Text(text, color = color, style = style, maxLines = maxLines, overflow = TextOverflow.Ellipsis)
    }
}

/**
 * Text that may start with a status mark ("✓ ", "✗ " or "⚠ ", as stored in chats and tool results):
 * the mark is drawn as a line icon instead.
 */
@Composable
fun StatusLine(text: String, modifier: Modifier = Modifier, color: Color = LocalContentColor.current,
               style: TextStyle = TextStyle.Default, okColor: Color = color, badColor: Color = color) {
    val t = text.trimStart()
    when {
        t.startsWith("✓") -> IconText(Icons.Outlined.CheckCircle, t.drop(1).trim(), modifier, okColor, style)
        t.startsWith("✗") -> IconText(Icons.Outlined.Cancel, t.drop(1).trim(), modifier, badColor, style)
        t.startsWith("⚠") -> IconText(Icons.Outlined.WarningAmber, t.drop(1).trim(), modifier, badColor, style)
        else -> Text(text, modifier, color = color, style = style)
    }
}
