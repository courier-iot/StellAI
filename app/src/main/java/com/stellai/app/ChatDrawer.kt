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

// The chat screen's side drawer.

/** The side drawer: new chat, search past chats, projects, chats, and links to the other screens. */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
internal fun ColumnScope.ChatDrawerContent(
    app: App,
    search: String,
    onSearch: (String) -> Unit,
    searchHits: List<org.json.JSONObject>,
    searching: Boolean,
    searchError: String?,
    onClose: () -> Unit,
    onDelete: (Conversation) -> Unit,
    onOpenProject: (String) -> Unit,
    onOpenTasks: () -> Unit,
    onOpenServer: () -> Unit,
    onOpenAvatar: () -> Unit,
) {
    val engine = app.engine
    IconText(
        LineIcons.Sparkle, "MISSION LOG",
        style = HudLabel.copy(fontSize = 14.sp, letterSpacing = 3.sp),
        color = Space.Accent,
        modifier = Modifier.padding(20.dp, 22.dp, 20.dp, 12.dp),
    )
    val itemColors = NavigationDrawerItemDefaults.colors(
        selectedContainerColor = Space.Accent.copy(alpha = 0.14f),
        unselectedContainerColor = Color.Transparent,
        selectedTextColor = Space.Accent,
        selectedIconColor = Space.Accent,
    )
    NavigationDrawerItem(
        label = { Text("New chat") },
        icon = { Icon(Icons.Filled.Add, contentDescription = null) },
        selected = false,
        onClick = { engine.newChat(); onClose() },
        colors = itemColors,
        modifier = Modifier.padding(horizontal = 12.dp),
    )
    OutlinedTextField(
        value = search,
        onValueChange = onSearch,
        singleLine = true,
        placeholder = { Text("Search past chats") },
        leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null, tint = Space.Muted) },
        trailingIcon = {
            if (search.isNotEmpty()) IconButton(onClick = { onSearch("") }) {
                Icon(Icons.Filled.Close, contentDescription = "Clear search", tint = Space.Muted)
            }
        },
        modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp),
    )
    HorizontalDivider(Modifier.padding(vertical = 8.dp), color = Space.Line)
    if (search.isNotBlank()) {
        val q = search.trim()
        val titleHits = engine.conversations.filter { it.title.contains(q, ignoreCase = true) }
        val serverIds = searchHits.map { it.optString("conversation_id") }.toSet()
        LazyColumn(Modifier.weight(1f)) {
            if (searching) item(key = "s-busy") {
                Text("Searching…", color = Space.Muted, style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(start = 28.dp, bottom = 6.dp))
            }
            searchError?.let { e -> item(key = "s-err") {
                Text(e, color = Space.Gold, style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(start = 28.dp, end = 16.dp, bottom = 6.dp))
            } }
            items(searchHits, key = { "s-" + it.optString("conversation_id") }) { h ->
                val id = h.optString("conversation_id")
                NavigationDrawerItem(
                    label = {
                        Column(Modifier.padding(vertical = 4.dp)) {
                            Text(h.optString("title").ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            Text(h.optString("date"), style = HudLabel.copy(fontSize = 10.sp), color = Space.Muted)
                            Text(h.optString("excerpt"), style = MaterialTheme.typography.bodySmall, color = Space.Muted,
                                maxLines = 3, overflow = TextOverflow.Ellipsis)
                        }
                    },
                    selected = id == engine.currentId,
                    onClick = { engine.open(id); onSearch(""); onClose() },
                    colors = itemColors,
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
            }
            items(titleHits.filter { it.id !in serverIds }, key = { "t-" + it.id }) { c ->
                NavigationDrawerItem(
                    label = { Text(c.title.ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    selected = c.id == engine.currentId,
                    onClick = { engine.open(c.id); onSearch(""); onClose() },
                    colors = itemColors,
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
            }
            if (!searching && searchHits.isEmpty() && titleHits.isEmpty()) item(key = "s-none") {
                Text(if (q.length < 3) "Keep typing…" else "No chats found.", color = Space.Muted,
                    modifier = Modifier.padding(start = 28.dp))
            }
        }
    } else LazyColumn(Modifier.weight(1f)) {
        item(key = "projects-header") {
            Row(
                Modifier.fillMaxWidth().padding(start = 28.dp, end = 16.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("PROJECTS", style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 2.sp), color = Space.Muted,
                    modifier = Modifier.weight(1f))
                IconButton(onClick = { onOpenProject("new"); onClose() }) {
                    Icon(Icons.Filled.Add, contentDescription = "New project", tint = Space.Accent, modifier = Modifier.size(20.dp))
                }
            }
        }
        if (app.projects.items.isEmpty()) {
            item(key = "projects-empty") {
                Text(
                    "Group chats with shared instructions, notes and files. Tap + to start one.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                    modifier = Modifier.padding(start = 28.dp, end = 20.dp, bottom = 8.dp),
                )
            }
        }
        items(app.projects.items, key = { "p-" + it.id }) { p ->
            val color = accentFor(p.color).main
            NavigationDrawerItem(
                label = { Text(p.name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                icon = { Icon(Icons.Outlined.Folder, contentDescription = null, tint = color, modifier = Modifier.size(18.dp)) },
                selected = false,
                onClick = { onOpenProject(p.id); onClose() },
                badge = {
                    val n = engine.conversations.count { it.projectId == p.id }
                    if (n > 0) Text("$n", style = HudLabel.copy(fontSize = 11.sp), color = Space.Muted)
                },
                colors = itemColors,
                modifier = Modifier.padding(horizontal = 12.dp),
            )
        }
        item(key = "chats-header") {
            Text("CHATS", style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 2.sp), color = Space.Muted,
                modifier = Modifier.padding(start = 28.dp, top = 14.dp, bottom = 6.dp))
        }
        items(engine.conversations, key = { it.id }) { c ->
            val cp = app.projects.get(c.projectId)
            NavigationDrawerItem(
                label = {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            if (c.mode.isNotEmpty()) {
                                Icon(LineIcons.forMode(app.modes.get(c.mode).icon), contentDescription = null,
                                    tint = Space.Muted, modifier = Modifier.size(14.dp))
                                Spacer(Modifier.size(6.dp))
                            }
                            Text(c.title.ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                        if (cp != null) {
                            IconText(Icons.Outlined.Folder, cp.name, style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 0.5.sp),
                                color = accentFor(cp.color).main.copy(alpha = 0.85f), size = 11.dp, maxLines = 1)
                        }
                    }
                },
                selected = c.id == engine.currentId,
                onClick = { engine.open(c.id); onClose() },
                badge = {
                    IconButton(onClick = { onDelete(c) }, modifier = Modifier.size(32.dp)) {
                        Icon(Icons.Filled.Delete, contentDescription = "Delete chat", modifier = Modifier.size(18.dp), tint = Space.Muted)
                    }
                },
                colors = itemColors,
                modifier = Modifier.padding(horizontal = 12.dp),
            )
        }
    }
    HorizontalDivider(color = Space.Line)
    Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp)) {
        TextButton(onClick = { onOpenProject(ProjectManager.LIBRARY); onClose() }) { IconText(Icons.Outlined.LocalLibrary, "Library", color = Space.Accent) }
        TextButton(onClick = { onOpenTasks(); onClose() }) { IconText(LineIcons.Reminder, "Reminders", color = Space.Accent) }
    }
    Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp)) {
        TextButton(onClick = { onOpenServer(); onClose() }) { IconText(Icons.Outlined.Dns, "Server", color = Space.Accent) }
        TextButton(onClick = { onOpenAvatar(); onClose() }) { IconText(LineIcons.Sparkle, "Avatar screen", color = Space.Accent) }
    }
    Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        val sync = app.sync
        val label = when {
            sync.status.startsWith("Sync failed") -> sync.status
            sync.lastSuccess > 0 -> "Synced " + DateUtils.getRelativeTimeSpanString(
                sync.lastSuccess, System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS,
            )
            else -> sync.status
        }
        Text(label, style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 0.5.sp), color = Space.Muted,
            modifier = Modifier.weight(1f), maxLines = 2)
        IconButton(onClick = { sync.request() }) { Icon(Icons.Filled.Refresh, contentDescription = "Sync now", tint = Space.Accent) }
    }
}
