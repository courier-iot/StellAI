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
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.union
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
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
fun ChatScreen(onOpenSettings: () -> Unit) {
    val app = App.instance
    val engine = app.engine
    val voice = app.voice
    val name = app.settings.assistantName.ifBlank { "Stella" }
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val listState = rememberLazyListState()
    var confirmDelete by remember { mutableStateOf<Conversation?>(null) }

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
                Text(
                    "✦ MISSION LOG",
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
                HorizontalDivider(Modifier.padding(vertical = 8.dp), color = Space.Line)
                LazyColumn(Modifier.weight(1f)) {
                    items(engine.conversations, key = { it.id }) { c ->
                        NavigationDrawerItem(
                            label = { Text(c.title.ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis) },
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
                        },
                    )
                },
                bottomBar = {
                    Column(
                        Modifier
                            .background(Space.Void.copy(alpha = 0.7f))
                            .windowInsetsPadding(WindowInsets.ime.union(WindowInsets.navigationBars))
                    ) {
                        HorizontalDivider(color = Space.Accent.copy(alpha = 0.18f))
                        app.memory.lastLearned?.let { note ->
                            Text(
                                "✦ $note",
                                style = HudLabel.copy(fontSize = 11.sp, letterSpacing = 0.5.sp),
                                color = Space.Accent,
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { app.memory.lastLearned = null }
                                    .padding(horizontal = 16.dp, vertical = 6.dp),
                            )
                        }
                        notice?.let { n ->
                            Text(
                                "⚠ $n  (tap to dismiss)",
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
                                Text(
                                    "✎ Editing a message — sending replaces it and the replies after it",
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
                                        ) { Text("✕", fontSize = 11.sp, color = Space.Text) }
                                    }
                                }
                                if (preparingPhoto) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp, color = Space.Accent)
                            }
                        }
                        voice.lastError?.let { err ->
                            Text(
                                "⚠ $err  (tap to dismiss)",
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
                                    Text("📷", style = MaterialTheme.typography.titleMedium)
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
                                        Text("🎙", style = MaterialTheme.typography.titleMedium)
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
                    EmptyState(Modifier.padding(padding), app, name)
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
private fun EmptyState(modifier: Modifier, app: App, name: String) {
    Box(modifier.fillMaxSize().padding(32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("✦", fontSize = 44.sp, color = Space.Accent)
            Spacer(Modifier.size(8.dp))
            Text(
                "${name.uppercase()} ONLINE",
                style = HudLabel.copy(fontSize = 18.sp, letterSpacing = 5.sp),
                color = Space.Text,
            )
            Spacer(Modifier.size(16.dp))
            val wake = if (app.assistantRunning) "Say “${app.settings.wakeWordLabel}, set an alarm for 6:45”\nor tap 🎙\n\n" else "Tap 🎙 and speak, or type:\n"
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
                    Text(
                        "› ${lines.filterNot { DIAGRAM_LINE.matches(it.trim()) }.joinToString(" ").trim()}",
                        style = HudLabel.copy(letterSpacing = 0.5.sp),
                        color = Space.Gold,
                        modifier = longPress,
                    )
                    Actions()
                }
                if (diagram != null) {
                    Spacer(Modifier.size(6.dp))
                    RemoteImage("diagram:" + diagram.groupValues[2], diagram.groupValues[1])
                }
            }
        }
        Role.ERROR -> Text(
            "⚠ ${msg.text}",
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
                        Text(
                            "✦ ${name.uppercase()}",
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
    Text(
        text = if (expanded) reasoning.trim() else "◌ Thought process (tap to show)",
        style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
        fontStyle = FontStyle.Italic,
        color = Space.Glow.copy(alpha = 0.85f),
        modifier = Modifier
            .clickable { expanded = !expanded }
            .padding(bottom = 6.dp),
    )
}
