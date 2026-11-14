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

// The chat screen's dialogs: move to a project, pick a mode, memory use, delete, approve an action.

@Composable
internal fun ProjectChoice(label: String, color: Color, selected: Boolean, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 12.dp, horizontal = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(12.dp).border(2.dp, color, CircleShape))
        Spacer(Modifier.size(10.dp))
        Text(label, color = if (selected) Space.Accent else Space.Text, modifier = Modifier.weight(1f))
        if (selected) Icon(Icons.Outlined.Check, contentDescription = "Selected", tint = Space.Accent)
    }
}

/** Asks before Stella changes anything on GitHub. Shows exactly what would be committed. */
@Composable
internal fun ApprovalDialog(a: Approval, name: String) {
    val args = a.args
    val repo = args.optString("repo").ifBlank { "your default repository" }
    AlertDialog(
        onDismissRequest = { a.decline() },
        containerColor = Space.Panel,
        title = { Text(if (a.tool == "github_commit") "Approve this commit?" else "Open this pull request?") },
        text = {
            LazyColumn(Modifier.heightIn(max = 460.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (a.tool == "github_commit") {
                    item {
                        Text("$name wants to commit to $repo, branch “${args.optString("branch")}”:")
                        Text(args.optString("message"), fontStyle = FontStyle.Italic, color = Space.Muted)
                    }
                    val files = args.optJSONArray("files") ?: org.json.JSONArray()
                    items((0 until files.length()).toList()) { i ->
                        val f = files.optJSONObject(i) ?: org.json.JSONObject()
                        var open by remember { mutableStateOf(false) }
                        val content = f.optString("content")
                        Column(Modifier.fillMaxWidth().border(1.dp, Space.Line, RoundedCornerShape(8.dp)).clickable { open = !open }.padding(8.dp)) {
                            Text("${if (open) "▾" else "▸"} ${f.optString("path")}  (${content.lines().size} lines)", color = Space.Accent)
                            if (open) Text(content.take(20000), fontFamily = FontFamily.Monospace, fontSize = 11.sp, color = Space.Text)
                        }
                    }
                    val deletes = args.optJSONArray("delete")
                    if (deletes != null && deletes.length() > 0) item {
                        Text("Delete: " + (0 until deletes.length()).joinToString { deletes.optString(it) }, color = Space.Red)
                    }
                    item {
                        Text("Nothing goes to your main branch; review it on GitHub and merge with a pull request.",
                            style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                    }
                } else {
                    item {
                        Text("$name wants to open a pull request in $repo from “${args.optString("branch")}”:")
                        Text(args.optString("title"), color = Space.Accent)
                        MarkdownContent(args.optString("body"), Space.Text)
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = { a.approve() }) { Text("Approve") } },
        dismissButton = { TextButton(onClick = { a.decline() }) { Text("Decline", color = Space.Red) } },
    )
}

/** Project chip and context meter under the top bar. */
@Composable
internal fun ContextStrip(
    mode: Mode,
    project: Project?,
    info: ContextInfo?,
    onMode: () -> Unit,
    onProject: () -> Unit,
    onMeter: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(Space.Void.copy(alpha = 0.55f))
            .padding(horizontal = 12.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Mode chip: tap to switch mindset
        Row(
            Modifier
                .border(1.dp, Space.Accent.copy(alpha = 0.45f), RoundedCornerShape(50))
                .clickable(onClick = onMode)
                .padding(horizontal = 10.dp, vertical = 3.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(LineIcons.forMode(mode.icon), contentDescription = null, tint = Space.Accent, modifier = Modifier.size(13.dp))
            Spacer(Modifier.size(5.dp))
            Text(mode.name.uppercase(), style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 1.sp), color = Space.Accent, maxLines = 1)
            Icon(Icons.Filled.ArrowDropDown, contentDescription = null, tint = Space.Accent, modifier = Modifier.size(16.dp))
        }
        Spacer(Modifier.size(10.dp))
        if (project != null) {
            IconText(
                Icons.Outlined.Folder, project.name,
                style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 0.5.sp),
                color = accentFor(project.color).main, size = 12.dp, maxLines = 1,
                modifier = Modifier.weight(1f, fill = false).clickable(onClick = onProject).padding(vertical = 4.dp),
            )
            Spacer(Modifier.size(12.dp))
        }
        Spacer(Modifier.weight(0.01f))
        if (info != null) {
            val f = info.fraction
            val color = when {
                f > 0.85f -> Space.Red
                f > 0.6f -> Space.Gold
                else -> Space.Accent
            }
            Row(
                Modifier.clickable(onClick = onMeter).padding(vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("CONTEXT", style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 1.5.sp), color = Space.Muted)
                Spacer(Modifier.size(6.dp))
                LinearProgressIndicator(
                    progress = { f },
                    modifier = Modifier.size(width = 64.dp, height = 4.dp),
                    color = color,
                    trackColor = Space.Line,
                )
                Spacer(Modifier.size(6.dp))
                Text("${(f * 100).toInt()}%", style = HudLabel.copy(fontSize = 10.sp), color = color)
            }
        }
    }
}

private fun tokens(n: Int): String = if (n >= 1000) "%.1fk".format(n / 1000f) else n.toString()

/** Move this chat to a project (or start it in one). */
@Composable
internal fun MoveChatDialog(app: App, project: Project?, onOpenProject: (String) -> Unit, onDismiss: () -> Unit) {
    val engine = app.engine
    AlertDialog(
        onDismissRequest = { onDismiss() },
        containerColor = Space.Panel,
        title = { Text(if (engine.currentId == null) "Start this chat in…" else "Move chat to…") },
        text = {
            LazyColumn(Modifier.heightIn(max = 380.dp)) {
                item {
                    ProjectChoice("No project", Space.Muted, selected = project == null) {
                        engine.moveToProject(engine.currentId, ""); onDismiss()
                    }
                }
                items(app.projects.items, key = { it.id }) { p ->
                    ProjectChoice(p.name, accentFor(p.color).main, selected = p.id == project?.id) {
                        engine.moveToProject(engine.currentId, p.id); onDismiss()
                    }
                }
                item {
                    ProjectChoice("+ New project…", Space.Accent, selected = false) {
                        onDismiss(); onOpenProject("new")
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = { onDismiss() }) { Text("Close") } },
    )
}

/** Pick this chat's mode. */
@Composable
internal fun ModePickerDialog(app: App, name: String, mode: Mode, onOpenModes: () -> Unit, onDismiss: () -> Unit) {
    val engine = app.engine
    AlertDialog(
        onDismissRequest = { onDismiss() },
        containerColor = Space.Panel,
        title = { Text(if (engine.currentId == null) "Mode for this chat" else "Switch mode") },
        text = {
            LazyColumn(Modifier.heightIn(max = 440.dp)) {
                item {
                    Text(
                        "Changes how $name thinks and writes in this chat, from her next reply on.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Space.Muted,
                        modifier = Modifier.padding(bottom = 8.dp),
                    )
                }
                items(app.modes.all, key = { it.id }) { m ->
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .clickable { engine.setMode(m.id); onDismiss() }
                            .padding(vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(LineIcons.forMode(m.icon), contentDescription = null, tint = Space.Accent,
                            modifier = Modifier.padding(end = 12.dp).size(22.dp))
                        Column(Modifier.weight(1f)) {
                            Text(m.name, color = if (m.id == mode.id) Space.Accent else Space.Text)
                            Text(m.description, style = MaterialTheme.typography.bodySmall, color = Space.Muted, maxLines = 2)
                        }
                        if (m.id == mode.id) Icon(Icons.Outlined.Check, contentDescription = "Current", tint = Space.Accent)
                    }
                }
            }
        },
        confirmButton = { TextButton(onClick = { onDismiss(); onOpenModes() }) { Text("Manage modes") } },
        dismissButton = { TextButton(onClick = { onDismiss() }) { Text("Close") } },
    )
}

/** How full Stella's memory is for this chat, with Refresh now and New chat from here. */
@Composable
internal fun ContextDialog(app: App, name: String, onDismiss: () -> Unit) {
    val engine = app.engine
    val info = engine.context
    AlertDialog(
        onDismissRequest = { onDismiss() },
        containerColor = Space.Panel,
        title = { Text("$name's memory for this chat") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (info != null) {
                    Text("${tokens(info.used)} of ${tokens(info.limit)} tokens used (${(info.fraction * 100).toInt()}%)" +
                        if (info.projectTokens > 0) ", including ${tokens(info.projectTokens)} of project context." else ".")
                }
                Text(
                    "Long chats don't fill up: while $name is idle, the server folds the oldest part of the chat " +
                        "into a summary (from about 60% full), and the newest part stays word for word. She can " +
                        "look up the exact earlier words when she needs them, and pinned notes are never summarized. " +
                        "You can also refresh now, e.g. when switching topics. Every message stays here and on " +
                        "your other devices.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = engine.currentId != null && !engine.busy,
                onClick = { onDismiss(); engine.refreshContext() },
            ) { Text("Refresh now") }
        },
        dismissButton = {
            Row {
                TextButton(
                    enabled = engine.currentId != null && !engine.busy,
                    onClick = { onDismiss(); engine.continueInNewChat() },
                ) { Text("New chat from here") }
                TextButton(onClick = { onDismiss() }) { Text("Close") }
            }
        },
    )
}

/** Delete a chat everywhere, after asking. */
@Composable
internal fun DeleteChatDialog(app: App, c: Conversation, onDismiss: () -> Unit) {
    val engine = app.engine
    AlertDialog(
        onDismissRequest = { onDismiss() },
        containerColor = Space.Panel,
        title = { Text("Delete chat?") },
        text = { Text("“${c.title}” will be deleted on all your devices.") },
        confirmButton = {
            TextButton(onClick = { engine.delete(c.id); onDismiss() }) { Text("Delete", color = Space.Red) }
        },
        dismissButton = {
            TextButton(onClick = { onDismiss() }) { Text("Cancel") }
        },
    )
}
