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

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun ChatScreen(
    onOpenSettings: () -> Unit,
    onOpenProject: (String) -> Unit,
    onOpenModes: () -> Unit,
    onOpenTasks: () -> Unit,
    onOpenServer: () -> Unit = {},
    onOpenAvatar: () -> Unit = {},
) {
    val app = App.instance
    val engine = app.engine
    val voice = app.voice
    val name = app.settings.assistantName.ifBlank { "Stella" }
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val listState = rememberLazyListState()
    var confirmDelete by remember { mutableStateOf<Conversation?>(null) }
    var moveDialog by remember { mutableStateOf(false) }
    var contextDialog by remember { mutableStateOf(false) }
    var modeDialog by remember { mutableStateOf(false) }
    val mode = engine.currentMode
    var overflow by remember { mutableStateOf(false) }
    val project = app.projects.get(engine.currentProjectId)
    var search by remember { mutableStateOf("") }
    var searchHits by remember { mutableStateOf<List<org.json.JSONObject>>(emptyList()) }
    var searching by remember { mutableStateOf(false) }
    var searchError by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(search) {
        val q = search.trim()
        if (q.length < 3) { searchHits = emptyList(); searchError = null; return@LaunchedEffect }
        kotlinx.coroutines.delay(450)   // wait for typing to pause
        searching = true
        try {
            val arr = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                HubApi.searchChats(app.settings.serverUrl, app.settings.apiKey, q)
            }
            searchHits = (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }
            searchError = null
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            searchHits = emptyList()
            searchError = "Server search unavailable; showing title matches only."
        } finally {
            searching = false
        }
    }
    LaunchedEffect(Unit) {
        engine.updateMeter()
        app.projects.refresh()
        app.modes.refresh()
    }

    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) voice.listenNow()
    }

    // Follow the reply as it streams in.
    val lastLength = engine.messages.lastOrNull()?.text?.length ?: 0
    LaunchedEffect(engine.messages.size, lastLength) {
        if (engine.messages.isNotEmpty()) listState.scrollToItem(engine.messages.lastIndex)
    }

    val context = LocalContext.current
    var notice by remember { mutableStateOf<String?>(null) }
    var preparingPhoto by remember { mutableStateOf(false) }
    var attachMenu by remember { mutableStateOf(false) }
    var cameraUri by remember { mutableStateOf<Uri?>(null) }

    fun addPhoto(uri: Uri?) {
        if (uri == null) return
        preparingPhoto = true
        scope.launch {
            try {
                engine.addPhoto(withContext(Dispatchers.IO) { Photos.prepare(context, uri) })
            } catch (e: Exception) {
                notice = "Couldn't use that photo: ${e.message ?: "unknown error"}"
            } finally {
                preparingPhoto = false
            }
        }
    }

    val pickPhoto = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri -> addPhoto(uri) }
    val takePhoto = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
        if (ok) addPhoto(cameraUri)
    }

    fun submit() {
        if ((engine.draft.isNotBlank() || engine.attachments.isNotEmpty()) && !engine.busy && !preparingPhoto) {
            engine.send(engine.draft)
            engine.draft = ""
        }
    }

    fun closeDrawer() {
        scope.launch { drawerState.close() }
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ModalDrawerSheet(drawerContainerColor = Space.Panel) {
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
                    onClick = { engine.newChat(); closeDrawer() },
                    colors = itemColors,
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
                OutlinedTextField(
                    value = search,
                    onValueChange = { search = it },
                    singleLine = true,
                    placeholder = { Text("Search past chats") },
                    leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null, tint = Space.Muted) },
                    trailingIcon = {
                        if (search.isNotEmpty()) IconButton(onClick = { search = "" }) {
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
                                onClick = { engine.open(id); search = ""; closeDrawer() },
                                colors = itemColors,
                                modifier = Modifier.padding(horizontal = 12.dp),
                            )
                        }
                        items(titleHits.filter { it.id !in serverIds }, key = { "t-" + it.id }) { c ->
                            NavigationDrawerItem(
                                label = { Text(c.title.ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                                selected = c.id == engine.currentId,
                                onClick = { engine.open(c.id); search = ""; closeDrawer() },
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
                            IconButton(onClick = { onOpenProject("new"); closeDrawer() }) {
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
                            onClick = { onOpenProject(p.id); closeDrawer() },
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
                            onClick = { engine.open(c.id); closeDrawer() },
                            badge = {
                                IconButton(onClick = { confirmDelete = c }, modifier = Modifier.size(32.dp)) {
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
                    TextButton(onClick = { onOpenProject(ProjectManager.LIBRARY); closeDrawer() }) { IconText(Icons.Outlined.LocalLibrary, "Library", color = Space.Accent) }
                    TextButton(onClick = { onOpenTasks(); closeDrawer() }) { IconText(LineIcons.Reminder, "Reminders", color = Space.Accent) }
                }
                Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp)) {
                    TextButton(onClick = { onOpenServer(); closeDrawer() }) { IconText(Icons.Outlined.Dns, "Server", color = Space.Accent) }
                    TextButton(onClick = { onOpenAvatar(); closeDrawer() }) { IconText(LineIcons.Sparkle, "Avatar screen", color = Space.Accent) }
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
        },
    ) {
        Box(Modifier.fillMaxSize()) {
            Starfield()
            Scaffold(
                containerColor = Color.Transparent,
                topBar = {
                  Column {
                    TopAppBar(
                        colors = TopAppBarDefaults.topAppBarColors(containerColor = Space.Void.copy(alpha = 0.55f)),
                        navigationIcon = {
                            IconButton(onClick = { scope.launch { drawerState.open() } }) {
                                Icon(Icons.Filled.Menu, contentDescription = "Chats", tint = Space.Accent)
                            }
                        },
                        title = {
                            Column {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    StatusDot(online = engine.busy || voice.phase != VoicePhase.IDLE || app.assistantRunning)
                                    Spacer(Modifier.size(8.dp))
                                    Text(
                                        name.uppercase(),
                                        style = HudLabel.copy(fontSize = 16.sp, letterSpacing = 4.sp),
                                        color = Space.Text,
                                    )
                                }
                                val subtitle = when {
                                    engine.status.isNotEmpty() -> engine.status
                                    voice.phase == VoicePhase.LISTENING -> "Listening…"
                                    voice.phase == VoicePhase.TRANSCRIBING -> "Decoding transmission…"
                                    engine.currentTitle.isNotEmpty() -> engine.currentTitle
                                    app.assistantRunning -> "Say “${app.settings.wakeWordLabel}”"
                                    else -> "Standing by"
                                }
                                Text(
                                    subtitle,
                                    style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 0.5.sp),
                                    color = Space.Muted,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                        },
                        actions = {
                            IconButton(onClick = { engine.newChat() }) {
                                Icon(Icons.Filled.Add, contentDescription = "New chat", tint = Space.Accent)
                            }
                            IconButton(onClick = onOpenSettings) {
                                Icon(Icons.Filled.Settings, contentDescription = "Settings", tint = Space.Muted)
                            }
                            Box {
                                IconButton(onClick = { overflow = true }) {
                                    Icon(Icons.Filled.MoreVert, contentDescription = "More", tint = Space.Muted)
                                }
                                DropdownMenu(expanded = overflow, onDismissRequest = { overflow = false }) {
                                    DropdownMenuItem(
                                        text = { Text("Refresh context") },
                                        enabled = engine.currentId != null && !engine.busy,
                                        onClick = { overflow = false; engine.refreshContext() },
                                    )
                                    DropdownMenuItem(
                                        text = { Text(if (project == null) "Move to project…" else "Change project…") },
                                        onClick = { overflow = false; moveDialog = true },
                                    )
                                    if (project != null) {
                                        DropdownMenuItem(
                                            text = { Text("Project details") },
                                            onClick = { overflow = false; onOpenProject(project.id) },
                                        )
                                    }
                                }
                            }
                        },
                    )
                    ContextStrip(
                        mode = mode,
                        project = project,
                        info = engine.context,
                        onMode = { modeDialog = true },
                        onProject = { project?.let { onOpenProject(it.id) } },
                        onMeter = { contextDialog = true },
                    )
                  }
                },
                bottomBar = {
                    Column(
                        Modifier
                            .background(Space.Void.copy(alpha = 0.7f))
                            .windowInsetsPadding(WindowInsets.ime.union(WindowInsets.navigationBars))
                    ) {
                        HorizontalDivider(color = Space.Accent.copy(alpha = 0.18f))
                        app.memory.lastLearned?.let { note ->
                            IconText(
                                LineIcons.Sparkle, note, size = 13.dp,
                                style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 0.5.sp),
                                color = Space.Accent,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { app.memory.lastLearned = null }
                                    .padding(horizontal = 16.dp, vertical = 6.dp),
                            )
                        }
                        engine.contextNotice?.let { n ->
                            IconText(
                                LineIcons.Sparkle, "$n  (tap to dismiss)", size = 14.dp,
                                style = MaterialTheme.typography.bodySmall,
                                color = Space.Gold,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { engine.contextNotice = null }
                                    .padding(horizontal = 16.dp, vertical = 6.dp),
                            )
                        }
                        notice?.let { n ->
                            IconText(
                                Icons.Outlined.WarningAmber, "$n  (tap to dismiss)", size = 14.dp,
                                style = MaterialTheme.typography.bodySmall,
                                color = Space.Red,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { notice = null }
                                    .padding(horizontal = 16.dp, vertical = 6.dp),
                            )
                        }
                        if (engine.editingId != null) {
                            Row(
                                Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                IconText(
                                    Icons.Outlined.Edit, "Editing a message — sending replaces it and the replies after it", size = 14.dp,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Space.Gold,
                                    modifier = Modifier.weight(1f),
                                )
                                TextButton(onClick = { engine.cancelEdit() }) { Text("Cancel") }
                            }
                        }
                        if (engine.attachments.isNotEmpty() || preparingPhoto) {
                            Row(
                                Modifier
                                    .fillMaxWidth()
                                    .horizontalScroll(rememberScrollState())
                                    .padding(start = 12.dp, end = 12.dp, top = 8.dp),
                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                engine.attachments.forEach { id ->
                                    Box {
                                        RemoteImage("image:$id", "Photo", Modifier.size(64.dp), crop = true)
                                        Box(
                                            Modifier
                                                .align(Alignment.TopEnd)
                                                .size(22.dp)
                                                .background(Space.Void.copy(alpha = 0.8f), CircleShape)
                                                .clickable { engine.removePhoto(id) },
                                            contentAlignment = Alignment.Center,
                                        ) { Icon(Icons.Filled.Close, contentDescription = "Remove photo", tint = Space.Text, modifier = Modifier.size(13.dp)) }
                                    }
                                }
                                if (preparingPhoto) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp, color = Space.Accent)
                            }
                        }
                        voice.lastError?.let { err ->
                            IconText(
                                Icons.Outlined.WarningAmber, "$err  (tap to dismiss)", size = 14.dp,
                                style = MaterialTheme.typography.bodySmall,
                                color = Space.Red,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { voice.clearError() }
                                    .padding(horizontal = 16.dp, vertical = 6.dp),
                            )
                        }
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(start = 4.dp, end = 12.dp, top = 8.dp, bottom = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Box {
                                IconButton(onClick = { attachMenu = true }) {
                                    Icon(Icons.Outlined.PhotoCamera, contentDescription = "Add a photo", tint = Space.Accent)
                                }
                                DropdownMenu(expanded = attachMenu, onDismissRequest = { attachMenu = false }) {
                                    DropdownMenuItem(text = { Text("Take photo") }, onClick = {
                                        attachMenu = false
                                        try {
                                            val uri = Photos.cameraUri(context)
                                            cameraUri = uri
                                            takePhoto.launch(uri)
                                        } catch (e: Exception) {
                                            notice = "Couldn't open the camera: ${e.message}"
                                        }
                                    })
                                    DropdownMenuItem(text = { Text("Choose photo") }, onClick = {
                                        attachMenu = false
                                        pickPhoto.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
                                    })
                                }
                            }
                            OutlinedTextField(
                                value = engine.draft,
                                onValueChange = { engine.draft = it },
                                modifier = Modifier.weight(1f),
                                placeholder = { Text("Message $name…", color = Space.Muted) },
                                maxLines = 5,
                                shape = RoundedCornerShape(24.dp),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Space.Accent.copy(alpha = 0.8f),
                                    unfocusedBorderColor = Space.Line,
                                    focusedContainerColor = Space.Panel.copy(alpha = 0.85f),
                                    unfocusedContainerColor = Space.Panel.copy(alpha = 0.7f),
                                    cursorColor = Space.Accent,
                                ),
                                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                                keyboardActions = KeyboardActions(onSend = { submit() }),
                            )
                            Spacer(Modifier.size(10.dp))
                            val listening = voice.phase == VoicePhase.LISTENING
                            val capturing = listening || voice.phase == VoicePhase.TRANSCRIBING
                            Box(contentAlignment = Alignment.Center, modifier = Modifier.size(52.dp)) {
                                if (listening) PulseRing()
                                val glow = IconButtonDefaults.filledIconButtonColors(
                                    containerColor = Space.Accent,
                                    contentColor = Space.OnAccent,
                                )
                                when {
                                    engine.busy -> FilledIconButton(onClick = { engine.stop() }, colors = glow) {
                                        Icon(Icons.Filled.Close, contentDescription = "Stop")
                                    }
                                    capturing -> FilledIconButton(onClick = { voice.cancel() }, colors = glow) {
                                        Icon(Icons.Filled.Close, contentDescription = "Cancel listening")
                                    }
                                    engine.draft.isBlank() && engine.attachments.isEmpty() -> FilledIconButton(onClick = {
                                        if (voice.hasMicPermission()) voice.listenNow()
                                        else micPermission.launch(Manifest.permission.RECORD_AUDIO)
                                    }, colors = glow) {
                                        Icon(Icons.Outlined.Mic, contentDescription = "Talk")
                                    }
                                    else -> FilledIconButton(onClick = { submit() }, colors = glow) {
                                        Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
                                    }
                                }
                            }
                        }
                    }
                },
            ) { padding ->
                if (engine.messages.isEmpty()) {
                    EmptyState(Modifier.padding(padding), app, name, project, mode)
                } else {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize().padding(padding),
                        contentPadding = PaddingValues(12.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        val lastReply = engine.messages.lastOrNull { it.role == Role.ASSISTANT }?.id
                        items(engine.messages, key = { it.id }) { msg ->
                            MessageRow(msg, name, canRetry = msg.id == lastReply && !engine.busy)
                        }
                    }
                }
            }
        }
    }

    if (moveDialog) {
        AlertDialog(
            onDismissRequest = { moveDialog = false },
            containerColor = Space.Panel,
            title = { Text(if (engine.currentId == null) "Start this chat in…" else "Move chat to…") },
            text = {
                LazyColumn(Modifier.heightIn(max = 380.dp)) {
                    item {
                        ProjectChoice("No project", Space.Muted, selected = project == null) {
                            engine.moveToProject(engine.currentId, ""); moveDialog = false
                        }
                    }
                    items(app.projects.items, key = { it.id }) { p ->
                        ProjectChoice(p.name, accentFor(p.color).main, selected = p.id == project?.id) {
                            engine.moveToProject(engine.currentId, p.id); moveDialog = false
                        }
                    }
                    item {
                        ProjectChoice("+ New project…", Space.Accent, selected = false) {
                            moveDialog = false; onOpenProject("new")
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { moveDialog = false }) { Text("Close") } },
        )
    }

    engine.pendingApproval?.let { a -> ApprovalDialog(a, name) }

    if (modeDialog) {
        AlertDialog(
            onDismissRequest = { modeDialog = false },
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
                                .clickable { engine.setMode(m.id); modeDialog = false }
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
            confirmButton = { TextButton(onClick = { modeDialog = false; onOpenModes() }) { Text("Manage modes") } },
            dismissButton = { TextButton(onClick = { modeDialog = false }) { Text("Close") } },
        )
    }

    if (contextDialog) {
        val info = engine.context
        AlertDialog(
            onDismissRequest = { contextDialog = false },
            containerColor = Space.Panel,
            title = { Text("$name's memory for this chat") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (info != null) {
                        Text("${tokens(info.used)} of ${tokens(info.limit)} tokens used (${(info.fraction * 100).toInt()}%)" +
                            if (info.projectTokens > 0) ", including ${tokens(info.projectTokens)} of project context." else ".")
                    }
                    Text(
                        "When this fills up, $name refreshes it automatically: she summarizes the older part of the chat " +
                            "and continues from the summary. You can also refresh now, e.g. when switching topics. " +
                            "Every message stays here and on your other devices.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Space.Muted,
                    )
                }
            },
            confirmButton = {
                TextButton(
                    enabled = engine.currentId != null && !engine.busy,
                    onClick = { contextDialog = false; engine.refreshContext() },
                ) { Text("Refresh now") }
            },
            dismissButton = { TextButton(onClick = { contextDialog = false }) { Text("Close") } },
        )
    }

    confirmDelete?.let { c ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            containerColor = Space.Panel,
            title = { Text("Delete chat?") },
            text = { Text("“${c.title}” will be deleted on all your devices.") },
            confirmButton = {
                TextButton(onClick = { engine.delete(c.id); confirmDelete = null }) { Text("Delete", color = Space.Red) }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = null }) { Text("Cancel") }
            },
        )
    }
}

/** Small glowing dot: cyan when Stella is active or listening, dim when idle. */
@Composable
private fun StatusDot(online: Boolean) {
    val color = if (online) Space.Accent else Space.Muted
    Box(
        Modifier
            .size(8.dp)
            .background(color, CircleShape)
            .border(3.dp, color.copy(alpha = 0.25f), CircleShape)
    )
}

/** Expanding ring behind the mic button while listening. Only exists while listening. */
@Composable
private fun PulseRing() {
    val pulse = rememberInfiniteTransition(label = "pulse")
    val scale by pulse.animateFloat(
        initialValue = 0.85f,
        targetValue = 1.3f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "scale",
    )
    Box(
        Modifier
            .size(48.dp)
            .scale(scale)
            .background(Space.Accent.copy(alpha = 0.22f), CircleShape)
    )
}

@Composable
private fun EmptyState(modifier: Modifier, app: App, name: String, project: Project?, mode: Mode) {
    Box(modifier.fillMaxSize().padding(32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            if (mode.id != GENERAL_MODE.id) {
                IconText(LineIcons.forMode(mode.icon), "${mode.name.uppercase()} MODE",
                    style = HudLabel.copy(fontSize = 13.sp, letterSpacing = 3.sp), color = Space.Glow)
                Spacer(Modifier.size(6.dp))
                Text(mode.description, textAlign = TextAlign.Center, color = Space.Muted, style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.size(24.dp))
            }
            if (project != null) {
                IconText(Icons.Outlined.Folder, project.name.uppercase(), style = HudLabel.copy(fontSize = 13.sp, letterSpacing = 3.sp),
                    color = accentFor(project.color).main)
                Spacer(Modifier.size(6.dp))
                Text(
                    "New chat in this project. $name knows its instructions, " +
                        "${project.notes.size} note${if (project.notes.size == 1) "" else "s"} and " +
                        "${project.files.size} file${if (project.files.size == 1) "" else "s"}.",
                    textAlign = TextAlign.Center,
                    color = Space.Muted,
                    style = MaterialTheme.typography.bodySmall,
                )
                Spacer(Modifier.size(28.dp))
            }
            Icon(LineIcons.Sparkle, contentDescription = null, tint = Space.Accent, modifier = Modifier.size(52.dp))
            Spacer(Modifier.size(8.dp))
            Text(
                "${name.uppercase()} ONLINE",
                style = HudLabel.copy(fontSize = 18.sp, letterSpacing = 5.sp),
                color = Space.Text,
            )
            Spacer(Modifier.size(16.dp))
            val wake = if (app.assistantRunning) "Say “${app.settings.wakeWordLabel}, set an alarm for 6:45”\nor tap the microphone\n\n" else "Tap the microphone and speak, or type:\n"
            Text(
                wake + "“Start a 10 minute timer”\n“Open Spotify”\n“Turn on the flashlight”",
                textAlign = TextAlign.Center,
                color = Space.Muted,
            )
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun MessageRow(msg: UiMessage, name: String, canRetry: Boolean) {
    val engine = App.instance.engine
    val clipboard = LocalClipboardManager.current
    val context = LocalContext.current
    var menu by remember { mutableStateOf(false) }
    val plain = msg.text.replace(MemoryManager.IMAGE_REF, "").trim()
    val longPress = Modifier.combinedClickable(onClick = {}, onLongClick = { menu = true })

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
        }
    }

    when (msg.role) {
        Role.TOOL -> {
            val lines = msg.text.lines()
            val diagram = lines.firstNotNullOfOrNull { DIAGRAM_LINE.find(it.trim()) }
            Column(Modifier.fillMaxWidth().padding(horizontal = 10.dp)) {
                Box {
                    StatusLine(
                        lines.filterNot { DIAGRAM_LINE.matches(it.trim()) }.joinToString(" ").trim(),
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
                            MarkdownContent(msg.text, Space.Text)
                        }
                    }
                    Actions()
                }
            }
        }
    }
}

@Composable
private fun ReasoningBlock(reasoning: String) {
    var expanded by remember { mutableStateOf(false) }
    val mod = Modifier.clickable { expanded = !expanded }.padding(bottom = 6.dp)
    val style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace, fontStyle = FontStyle.Italic)
    if (expanded) Text(reasoning.trim(), style = style, color = Space.Glow.copy(alpha = 0.85f), modifier = mod)
    else IconText(Icons.Outlined.Psychology, "Thought process (tap to show)", mod, Space.Glow.copy(alpha = 0.85f), style, 14.dp)
}

private val DIAGRAM_LINE = Regex("^!\\[([^\\]]*)\\]\\(diagram:([0-9a-f]{16})\\)$")

/** Marks where the context was refreshed; tap to read what Stella carried forward. */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SummaryCard(msg: UiMessage, name: String, onLongPress: () -> Unit, actions: @Composable () -> Unit) {
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

/** Project chip and context meter under the top bar. */
@Composable
private fun ContextStrip(
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

@Composable
private fun ProjectChoice(label: String, color: Color, selected: Boolean, onClick: () -> Unit) {
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

private fun tokens(n: Int): String = if (n >= 1000) "%.1fk".format(n / 1000f) else n.toString()

/** Asks before Stella changes anything on GitHub. Shows exactly what would be committed. */
@Composable
private fun ApprovalDialog(a: Approval, name: String) {
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
