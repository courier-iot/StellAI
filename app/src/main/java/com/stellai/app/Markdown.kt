package com.stellai.app

import android.graphics.Bitmap
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

// ---- Parsing --------------------------------------------------------------------------

sealed class MdBlock {
    data class Para(val text: String) : MdBlock()
    data class Heading(val level: Int, val text: String) : MdBlock()
    data class Code(val lang: String, val code: String) : MdBlock()
    data class Item(val marker: String, val indent: Int, val text: String) : MdBlock()
    data class Quote(val text: String) : MdBlock()
    data class Picture(val alt: String, val src: String) : MdBlock()
    data object Rule : MdBlock()
}

private val FENCE = Regex("^(```+|~~~+)\\s*([\\w+#.-]*)")
private val HEADING = Regex("^(#{1,6})\\s+(.*)")
private val RULE = Regex("^(-{3,}|\\*{3,}|_{3,})\\s*$")
private val IMAGE = Regex("^!\\[([^\\]]*)\\]\\(([^)\\s]+)\\)\\s*$")
private val BULLET = Regex("^(\\s*)[-*+]\\s+(.*)")
private val ORDERED = Regex("^(\\s*)(\\d+)[.)]\\s+(.*)")
private val QUOTE = Regex("^>\\s?(.*)")

/** A small Markdown parser: enough for chat replies (code, headings, lists, quotes, images). */
fun parseMarkdown(src: String): List<MdBlock> {
    val lines = src.replace("\r\n", "\n").split('\n')
    val out = mutableListOf<MdBlock>()
    val para = StringBuilder()
    fun flush() {
        if (para.isNotBlank()) out.add(MdBlock.Para(para.toString().trim()))
        para.clear()
    }
    var i = 0
    while (i < lines.size) {
        val line = lines[i]
        val t = line.trimStart()
        val fence = FENCE.find(t)
        when {
            fence != null -> {
                flush()
                val marker = fence.groupValues[1]
                val code = StringBuilder()
                i++
                while (i < lines.size && !lines[i].trimStart().startsWith(marker)) {
                    code.append(lines[i]).append('\n')
                    i++
                }
                out.add(MdBlock.Code(fence.groupValues[2], code.toString().trimEnd('\n')))
            }
            t.startsWith("|") -> {
                // Tables: shown as monospace text so columns stay lined up
                flush()
                val table = StringBuilder()
                while (i < lines.size && lines[i].trimStart().startsWith("|")) {
                    table.append(lines[i].trim()).append('\n')
                    i++
                }
                out.add(MdBlock.Code("table", table.toString().trimEnd('\n')))
                continue
            }
            t.isEmpty() -> flush()
            HEADING.matches(t) -> {
                flush()
                val m = HEADING.find(t)!!
                out.add(MdBlock.Heading(m.groupValues[1].length, m.groupValues[2].trim().trimEnd('#').trim()))
            }
            RULE.matches(t) -> {
                flush()
                out.add(MdBlock.Rule)
            }
            IMAGE.matches(t) -> {
                flush()
                val m = IMAGE.find(t)!!
                out.add(MdBlock.Picture(m.groupValues[1], m.groupValues[2]))
            }
            BULLET.matches(line) -> {
                flush()
                val m = BULLET.find(line)!!
                out.add(MdBlock.Item("•", m.groupValues[1].length / 2, m.groupValues[2]))
            }
            ORDERED.matches(line) -> {
                flush()
                val m = ORDERED.find(line)!!
                out.add(MdBlock.Item("${m.groupValues[2]}.", m.groupValues[1].length / 2, m.groupValues[3]))
            }
            QUOTE.matches(t) -> {
                flush()
                out.add(MdBlock.Quote(QUOTE.find(t)!!.groupValues[1]))
            }
            else -> {
                if (para.isNotEmpty()) para.append(' ')
                para.append(t)
            }
        }
        i++
    }
    flush()
    return out
}

private val LINK = Regex("^\\[([^\\]]+)\\]\\(([^)\\s]+)\\)")

/** Inline styles: **bold**, *italic*, `code`, [links](url). */
fun inlineMarkdown(text: String, accent: Color, codeBg: Color): AnnotatedString = buildAnnotatedString {
    fun walk(s: String) {
        var i = 0
        while (i < s.length) {
            val c = s[i]
            val prevWordChar = i > 0 && s[i - 1].isLetterOrDigit()
            when {
                (s.startsWith("**", i) || s.startsWith("__", i)) && !(c == '_' && prevWordChar) -> {
                    val end = s.indexOf(s.substring(i, i + 2), i + 2)
                    if (end > i + 2) {
                        withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { walk(s.substring(i + 2, end)) }
                        i = end + 2
                    } else {
                        append(c); i++
                    }
                }
                c == '`' -> {
                    val end = s.indexOf('`', i + 1)
                    if (end > i) {
                        withStyle(SpanStyle(fontFamily = FontFamily.Monospace, background = codeBg, color = accent)) {
                            append(s.substring(i + 1, end))
                        }
                        i = end + 1
                    } else {
                        append(c); i++
                    }
                }
                (c == '*' || (c == '_' && !prevWordChar)) && i + 1 < s.length && !s[i + 1].isWhitespace() -> {
                    val end = s.indexOf(c, i + 1)
                    val closesWord = end > i + 1 && !(c == '_' && end + 1 < s.length && s[end + 1].isLetterOrDigit())
                    if (closesWord) {
                        withStyle(SpanStyle(fontStyle = FontStyle.Italic)) { walk(s.substring(i + 1, end)) }
                        i = end + 1
                    } else {
                        append(c); i++
                    }
                }
                c == '[' -> {
                    val m = LINK.find(s.substring(i))
                    if (m != null && (m.groupValues[2].startsWith("http://") || m.groupValues[2].startsWith("https://"))) {
                        withLink(
                            LinkAnnotation.Url(
                                m.groupValues[2],
                                TextLinkStyles(SpanStyle(color = accent, textDecoration = TextDecoration.Underline)),
                            )
                        ) { append(m.groupValues[1]) }
                        i += m.value.length
                    } else {
                        append(c); i++
                    }
                }
                else -> {
                    append(c); i++
                }
            }
        }
    }
    walk(text)
}

// ---- Syntax highlighting --------------------------------------------------------------------

private val KEYWORDS = setOf(
    // C / C++ / Arduino / Java / Kotlin / JS / TS / Rust / Go / C#
    "if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue", "return",
    "goto", "struct", "union", "enum", "typedef", "const", "static", "volatile", "extern", "inline",
    "void", "int", "char", "float", "double", "long", "short", "signed", "unsigned", "bool", "auto",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t", "size_t",
    "class", "public", "private", "protected", "virtual", "override", "template", "typename", "namespace",
    "using", "new", "delete", "this", "true", "false", "nullptr", "NULL", "sizeof", "try", "catch",
    "throw", "import", "package", "fun", "val", "var", "object", "interface", "when", "is", "in",
    "null", "suspend", "data", "sealed", "open", "abstract", "final", "extends", "implements",
    "function", "let", "async", "await", "export", "from", "of", "typeof", "instanceof", "yield",
    "fn", "mut", "impl", "trait", "pub", "use", "mod", "match", "loop", "crate", "self", "Self",
    "func", "go", "chan", "defer", "range", "type", "map", "select", "string", "byte",
    // Python / shell
    "def", "lambda", "pass", "raise", "with", "as", "elif", "except", "finally", "global", "nonlocal",
    "not", "and", "or", "None", "True", "False", "print", "then", "fi", "done", "esac", "echo",
    "local", "export", "sudo",
)

private val HASH_COMMENT_LANGS = setOf(
    "python", "py", "bash", "sh", "shell", "zsh", "console", "yaml", "yml", "toml", "ruby", "rb",
    "perl", "r", "cmake", "makefile", "make", "dockerfile", "conf", "ini", "nim", "elixir", "micropython",
)

private val TOKEN = Regex(
    "(//[^\\n]*|/\\*[\\s\\S]*?\\*/)" +                         // 1 comment (C-style)
        "|(#[^\\n]*)" +                                        // 2 hash comment / preprocessor
        "|(\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*')" + // 3 string
        "|(\\b0[xX][0-9a-fA-F]+\\b|\\b\\d+(?:\\.\\d+)?(?:[eE][-+]?\\d+)?[fFuUlL]*\\b)" + // 4 number
        "|(\\b[A-Za-z_][A-Za-z0-9_]*\\b)"                      // 5 word
)

fun highlightCode(code: String, lang: String, accent: Color, glow: Color): AnnotatedString = buildAnnotatedString {
    val l = lang.lowercase()
    val hashComments = l in HASH_COMMENT_LANGS
    val comment = SpanStyle(color = Space.Muted, fontStyle = FontStyle.Italic)
    val string = SpanStyle(color = Space.Gold)
    val number = SpanStyle(color = glow)
    val keyword = SpanStyle(color = accent, fontWeight = FontWeight.SemiBold)
    val type = SpanStyle(color = Color(0xFF9FE6B8))
    val preprocessor = SpanStyle(color = glow)
    var last = 0
    if (l == "table" || l == "text" || l == "txt") {
        append(code)
        return@buildAnnotatedString
    }
    for (m in TOKEN.findAll(code)) {
        if (m.range.first > last) append(code.substring(last, m.range.first))
        val g = m.groups
        when {
            g[1] != null -> withStyle(comment) { append(m.value) }
            g[2] != null -> withStyle(if (hashComments) comment else preprocessor) { append(m.value) }
            g[3] != null -> withStyle(string) { append(m.value) }
            g[4] != null -> withStyle(number) { append(m.value) }
            m.value in KEYWORDS -> withStyle(keyword) { append(m.value) }
            m.value[0].isUpperCase() && m.value.length > 1 && m.value.any { it.isLowerCase() } ->
                withStyle(type) { append(m.value) }
            else -> append(m.value)
        }
        last = m.range.last + 1
    }
    if (last < code.length) append(code.substring(last))
}

// ---- Rendering ------------------------------------------------------------------------------

/** Renders a Markdown reply: paragraphs, headings, lists, quotes, code blocks and diagrams. */
@Composable
fun MarkdownContent(text: String, textColor: Color) {
    val accent = Space.Accent
    val codeBg = Space.PanelHigh
    val blocks = remember(text) { parseMarkdown(text) }
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (b in blocks) {
            when (b) {
                is MdBlock.Para -> Text(
                    remember(b.text, accent) { inlineMarkdown(b.text, accent, codeBg) },
                    color = textColor,
                )
                is MdBlock.Heading -> Text(
                    remember(b.text, accent) { inlineMarkdown(b.text, accent, codeBg) },
                    style = if (b.level <= 2) MaterialTheme.typography.titleMedium else MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Bold,
                    color = if (b.level <= 2) accent else textColor,
                )
                is MdBlock.Item -> Row(Modifier.padding(start = (b.indent * 14).dp)) {
                    Text(b.marker, color = accent, modifier = Modifier.widthIn(min = 18.dp).padding(end = 6.dp))
                    Text(remember(b.text, accent) { inlineMarkdown(b.text, accent, codeBg) }, color = textColor)
                }
                is MdBlock.Quote -> Row(Modifier.height(IntrinsicSize.Min)) {
                    Box(Modifier.width(3.dp).fillMaxHeight().background(accent.copy(alpha = 0.6f)))
                    Spacer(Modifier.width(8.dp))
                    Text(
                        remember(b.text, accent) { inlineMarkdown(b.text, accent, codeBg) },
                        color = Space.Muted,
                        fontStyle = FontStyle.Italic,
                    )
                }
                is MdBlock.Code -> CodeBlock(b.lang, b.code)
                is MdBlock.Picture -> if (b.src.startsWith("diagram:") || b.src.startsWith("image:")) {
                    RemoteImage(b.src, b.alt)
                } else {
                    Text("🖼 ${b.alt}", color = Space.Muted)
                }
                MdBlock.Rule -> HorizontalDivider(color = Space.Line)
            }
        }
    }
}

@Composable
fun CodeBlock(lang: String, code: String) {
    val clipboard = LocalClipboardManager.current
    var copied by remember { mutableStateOf(false) }
    LaunchedEffect(copied) {
        if (copied) {
            delay(1500)
            copied = false
        }
    }
    val accent = Space.Accent
    val glow = Space.Glow
    val highlighted = remember(code, lang, accent, glow) { highlightCode(code, lang, accent, glow) }
    val shape = RoundedCornerShape(10.dp)
    Column(
        Modifier
            .fillMaxWidth()
            .background(Color(0xFF060A14), shape)
            .border(1.dp, Space.Line, shape)
    ) {
        Row(Modifier.fillMaxWidth().padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(
                lang.ifBlank { "code" }.uppercase(),
                style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 1.5.sp),
                color = Space.Muted,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = {
                clipboard.setText(AnnotatedString(code))
                copied = true
            }) {
                Text(if (copied) "✓ Copied" else "Copy", style = HudLabel.copy(fontSize = 11.sp), color = accent)
            }
        }
        HorizontalDivider(color = Space.Line)
        Box(
            Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState())
                .padding(12.dp)
        ) {
            Text(
                highlighted,
                color = Space.Text,
                fontFamily = FontFamily.Monospace,
                fontSize = 12.5.sp,
                lineHeight = 18.sp,
                softWrap = false,
            )
        }
    }
}

/** A diagram or photo ("diagram:ID" / "image:ID"). Tap to view full screen with pinch-zoom. */
@Composable
fun RemoteImage(ref: String, title: String, modifier: Modifier = Modifier.fillMaxWidth(), crop: Boolean = false) {
    var bitmap by remember(ref) { mutableStateOf(MediaCache.peek(ref)) }
    var error by remember(ref) { mutableStateOf<String?>(null) }
    var zoom by remember { mutableStateOf(false) }
    val isPhoto = ref.startsWith("image:")
    LaunchedEffect(ref) {
        if (bitmap == null) {
            try {
                bitmap = withContext(Dispatchers.IO) { MediaCache.load(App.instance, ref) }
            } catch (e: Exception) {
                error = App.instance.engine.friendlyError(e)
            }
        }
    }
    val bmp = bitmap
    val shape = RoundedCornerShape(10.dp)
    when {
        bmp != null -> Image(
            bmp.asImageBitmap(),
            contentDescription = title,
            contentScale = if (crop) ContentScale.Crop else ContentScale.FillWidth,
            modifier = modifier
                .clip(shape)
                .background(if (isPhoto) Color.Black else Color.White)
                .border(1.dp, Space.Accent.copy(alpha = 0.35f), shape)
                .clickable { zoom = true },
        )
        error != null -> Text(
            "⚠ Couldn't load the ${if (isPhoto) "photo" else "diagram"}: $error",
            color = Space.Red,
            style = MaterialTheme.typography.bodySmall,
        )
        else -> Box(modifier.height(120.dp), contentAlignment = Alignment.Center) {
            CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp, color = Space.Accent)
        }
    }
    if (zoom && bmp != null) ZoomDialog(bmp, title) { zoom = false }
}

@Composable
private fun ZoomDialog(bitmap: Bitmap, title: String, onClose: () -> Unit) {
    var scale by remember { mutableFloatStateOf(1f) }
    var offset by remember { mutableStateOf(Offset.Zero) }
    Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Box(
            Modifier
                .fillMaxSize()
                .background(Space.Void.copy(alpha = 0.96f))
                .pointerInput(Unit) {
                    detectTransformGestures { _, pan, zoomChange, _ ->
                        scale = (scale * zoomChange).coerceIn(1f, 8f)
                        offset = if (scale == 1f) Offset.Zero else offset + pan
                    }
                },
            contentAlignment = Alignment.Center,
        ) {
            Image(
                bitmap.asImageBitmap(),
                contentDescription = title,
                contentScale = ContentScale.Fit,
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Color.White)
                    .graphicsLayer(scaleX = scale, scaleY = scale, translationX = offset.x, translationY = offset.y),
            )
            Row(
                Modifier.align(Alignment.TopStart).fillMaxWidth().padding(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(title, style = HudLabel.copy(fontSize = 13.sp), color = Space.Text, modifier = Modifier.weight(1f).padding(start = 8.dp))
                IconButton(onClick = onClose) { Icon(Icons.Filled.Close, contentDescription = "Close", tint = Space.Text) }
            }
        }
    }
}
