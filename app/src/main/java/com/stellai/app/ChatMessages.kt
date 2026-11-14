package com.stellai.app

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.text.format.DateUtils
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.union
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.filled.Search
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.outlined.Psychology
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material.icons.outlined.Mic
import androidx.compose.material.icons.outlined.PhotoCamera
import androidx.compose.material.icons.outlined.Edit
import androidx.compose.material.icons.outlined.WarningAmber
import androidx.compose.material.icons.outlined.Dns
import androidx.compose.material.icons.outlined.LocalLibrary
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.NavigationDrawerItemDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import androidx.compose.foundation.layout.ColumnScope

// The chat's messages: each message row (yours, Stella's, actions, summaries, errors) and its parts.

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun MessageRow(msg: UiMessage, name: String, canRetry: Boolean) {
    val engine = App.instance.engine
    val clipboard = LocalClipboardManager.current
    val context = LocalContext.current
    var menu by remember { mutableStateOf(false) }
    val plain = msg.text.replace(MemoryManager.IMAGE_REF, "").trim()
    // Auto mode's chip ("⇄ Auto: CAD Designer"): a tap opens the choices
    val autoChip = msg.role == Role.TOOL && msg.text.startsWith("⇄")
    val longPress = Modifier.combinedClickable(onClick = { if (autoChip) menu = true }, onLongClick = { menu = true })

    @Composable
    fun Actions() {
        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
            DropdownMenuItem(text = { Text("Copy") }, onClick = {
                clipboard.setText(AnnotatedString(plain))
                menu = false
            })
            DropdownMenuItem(text = { Text("Share") }, onClick = {
                menu = false
                val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, plain)
                context.startActivity(Intent.createChooser(send, "Share message"))
            })
            if (msg.role == Role.ASSISTANT && canRetry) {
                DropdownMenuItem(text = { Text("Retry") }, onClick = {
                    menu = false
                    engine.retry()
                })
            }
            if (msg.role == Role.USER && !engine.busy) {
                DropdownMenuItem(text = { Text("Edit & resend") }, onClick = {
                    menu = false
                    engine.startEdit(msg)
                })
            }
            if (autoChip && !engine.busy) {
                engine.autoModeNow?.let { now ->
                    DropdownMenuItem(text = { Text("Keep this chat in ${now.name}") }, onClick = {
                        menu = false
                        engine.pinMode(now.id)
                    })
                }
                AUTO_CHOICES.forEach { id ->
                    val m = App.instance.modes.get(id)
                    if (m.id != engine.autoModeNow?.id) {
                        DropdownMenuItem(text = { Text("Redo last reply in ${m.name}") }, onClick = {
                            menu = false
                            engine.redoAs(m.id)
                        })
                    }
                }
            }
        }
    }

    when (msg.role) {
        Role.TOOL -> {
            val (shown, actionButton) = ActionButtons.split(msg.text)
            val lines = shown.lines()
            val diagram = lines.firstNotNullOfOrNull { DIAGRAM_LINE.find(it.trim()) }
            val docRef = DOC_REF.find(shown)?.groupValues?.get(1)
            Column(Modifier.fillMaxWidth().padding(horizontal = 10.dp)) {
                Box {
                    StatusLine(
                        lines.filterNot { DIAGRAM_LINE.matches(it.trim()) }.joinToString(" ").replace(DOC_REF, "").trim(),
                        modifier = longPress,
                        color = Space.Gold, style = HudLabel.copy(letterSpacing = 0.5.sp),
                        okColor = Space.Gold, badColor = Space.Red,
                    )
                    Actions()
                }
                if (diagram != null) {
                    Spacer(Modifier.size(6.dp))
                    RemoteImage("diagram:" + diagram.groupValues[2], diagram.groupValues[1])
                }
                if (docRef != null) {
                    TextButton(onClick = { engine.openDocId = docRef }) { IconText(LineIcons.Document, "Open the document", color = Space.Accent) }
                }
                if (actionButton != null) {
                    TextButton(onClick = { engine.pressAction(msg.id) }) {
                        IconText(if (actionButton.label.startsWith("Cancel")) LineIcons.Cancel else LineIcons.Undo,
                            actionButton.label, color = Space.Accent)
                    }
                }
            }
        }
        Role.SUMMARY -> SummaryCard(msg, name, onLongPress = { menu = true }) { Actions() }
        Role.ERROR -> IconText(
            Icons.Outlined.WarningAmber, msg.text,
            color = MaterialTheme.colorScheme.onErrorContainer,
            modifier = Modifier
                .fillMaxWidth()
                .background(MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.85f), RoundedCornerShape(12.dp))
                .border(1.dp, Space.Red.copy(alpha = 0.5f), RoundedCornerShape(12.dp))
                .padding(12.dp),
        )
        Role.USER -> {
            val shape = RoundedCornerShape(18.dp, 18.dp, 4.dp, 18.dp)
            val photos = ChatEngine.PHOTO_REF.findAll(msg.text).map { it.groupValues[1] }.toList()
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                Box {
                    Column(
                        Modifier
                            .widthIn(max = 320.dp)
                            .background(
                                Brush.linearGradient(listOf(Space.Accent.copy(alpha = 0.30f), Space.Glow.copy(alpha = 0.30f))),
                                shape,
                            )
                            .border(1.dp, Space.Accent.copy(alpha = 0.45f), shape)
                            .then(longPress)
                            .padding(horizontal = 14.dp, vertical = 10.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        photos.forEach { id -> RemoteImage("image:$id", "Photo", Modifier.widthIn(max = 260.dp)) }
                        if (plain.isNotEmpty()) Text(plain, color = Space.Text)
                    }
                    Actions()
                }
            }
        }
        Role.ASSISTANT -> {
            val shape = RoundedCornerShape(18.dp, 18.dp, 18.dp, 4.dp)
            // Replies with code use the full width so code lines have room.
            val wide = msg.text.contains("```") || msg.text.contains("\n|")
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
                Box {
                    Column(
                        (if (wide) Modifier.fillMaxWidth() else Modifier.widthIn(max = 320.dp))
                            .background(Space.Panel.copy(alpha = 0.82f), shape)
                            .border(1.dp, Space.Accent.copy(alpha = 0.22f), shape)
                            .then(longPress)
                            .padding(horizontal = 14.dp, vertical = 10.dp)
                    ) {
                        IconText(
                            LineIcons.Sparkle, name.uppercase(), size = 12.dp,
                            style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 2.sp),
                            color = Space.Accent.copy(alpha = 0.8f),
                        )
                        Spacer(Modifier.size(4.dp))
                        if (msg.reasoning.isNotBlank()) ReasoningBlock(msg.reasoning)
                        if (msg.text.isEmpty()) {
                            CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = Space.Accent)
                        } else {
                            MarkdownContent(msg.text.replace(DOC_REF, "").replace(Regex(" +([.,;:!?])"), "$1").trim(), Space.Text)
                            DOC_REF.findAll(msg.text).map { it.groupValues[1] }.distinct().forEach { ref ->
                                TextButton(onClick = { engine.openDocId = ref }) { IconText(LineIcons.Document, "Open the document", color = Space.Accent) }
                            }
                        }
                    }
                    Actions()
                }
            }
        }
    }
}

@Composable
internal fun ReasoningBlock(reasoning: String) {
    var expanded by remember { mutableStateOf(false) }
    val mod = Modifier.clickable { expanded = !expanded }.padding(bottom = 6.dp)
    val style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace, fontStyle = FontStyle.Italic)
    if (expanded) Text(reasoning.trim(), style = style, color = Space.Glow.copy(alpha = 0.85f), modifier = mod)
    else IconText(Icons.Outlined.Psychology, "Thought process (tap to show)", mod, Space.Glow.copy(alpha = 0.85f), style, 14.dp)
}

internal val DOC_REF = Regex("\\[doc:([0-9a-f]{10})\\]")

internal val DIAGRAM_LINE = Regex("^!\\[([^\\]]*)\\]\\(diagram:([0-9a-f]{16})\\)$")

/** Marks where the context was refreshed; tap to read what Stella carried forward. */
@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun SummaryCard(msg: UiMessage, name: String, onLongPress: () -> Unit, actions: @Composable () -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val shape = RoundedCornerShape(12.dp)
    Box {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp)
                .background(Space.Glow.copy(alpha = 0.08f), shape)
                .border(1.dp, Space.Glow.copy(alpha = 0.35f), shape)
                .combinedClickable(onClick = { expanded = !expanded }, onLongClick = onLongPress)
                .padding(horizontal = 14.dp, vertical = 10.dp),
        ) {
            IconText(LineIcons.Sparkle, "CONTEXT REFRESHED", style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 2.sp),
                color = Space.Glow, size = 13.dp)
            Spacer(Modifier.size(4.dp))
            if (expanded) {
                MarkdownContent(msg.text, Space.Text)
            } else {
                Text(
                    "$name continues from a summary of the messages above. Tap to read it.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                )
            }
        }
        actions()
    }
}
