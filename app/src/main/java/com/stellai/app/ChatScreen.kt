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
    var notesDialog by remember { mutableStateOf(false) }
    var docsDialog by remember { mutableStateOf(false) }
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
                ChatDrawerContent(
                    app, search, { search = it }, searchHits, searching, searchError,
                    onClose = { closeDrawer() }, onDelete = { confirmDelete = it },
                    onOpenProject = onOpenProject, onOpenTasks = onOpenTasks, onOpenServer = onOpenServer, onOpenAvatar = onOpenAvatar,
                )
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
                                    StatusDot(online = engine.busy || voice.phase != VoicePhase.IDLE || app.wakeListening)
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
                                    app.wakeListening -> "Say “${app.settings.wakeWordLabel}”"
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
                                        text = { IconText(LineIcons.Pin, "Pinned notes") },
                                        enabled = engine.currentId != null,
                                        onClick = { overflow = false; notesDialog = true },
                                    )
                                    DropdownMenuItem(
                                        text = { IconText(LineIcons.Document, "Documents") },
                                        enabled = engine.currentId != null,
                                        onClick = { overflow = false; docsDialog = true },
                                    )
                                    DropdownMenuItem(
                                        text = { Text("Continue in a new chat") },
                                        enabled = engine.currentId != null && !engine.busy,
                                        onClick = { overflow = false; engine.continueInNewChat() },
                                    )
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

    if (moveDialog) MoveChatDialog(app, project, onOpenProject) { moveDialog = false }

    engine.pendingApproval?.let { a -> ApprovalDialog(a, name) }

    if (modeDialog) ModePickerDialog(app, name, mode, onOpenModes) { modeDialog = false }

    if (contextDialog) ContextDialog(app, name) { contextDialog = false }

    if (notesDialog) {
        engine.currentId?.let { id -> PinnedNotesDialog(app, id, name) { notesDialog = false } }
            ?: run { notesDialog = false }
    }

    if (docsDialog || engine.openDocId != null) {
        engine.currentId?.let { id ->
            DocsDialog(app, id, name, engine.openDocId, engine.docsChanged) { docsDialog = false; engine.openDocId = null }
        } ?: run { docsDialog = false; engine.openDocId = null }
    }

    confirmDelete?.let { c -> DeleteChatDialog(app, c) { confirmDelete = null } }
}
