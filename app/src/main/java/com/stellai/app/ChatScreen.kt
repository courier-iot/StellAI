package com.stellai.app

import android.Manifest
import android.text.format.DateUtils
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.foundation.text.selection.SelectionContainer
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

@OptIn(ExperimentalMaterial3Api::class)
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

    fun submit() {
        if (engine.draft.isNotBlank() && !engine.busy) {
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
                    color = Space.Cyan,
                    modifier = Modifier.padding(20.dp, 22.dp, 20.dp, 12.dp),
                )
                val itemColors = NavigationDrawerItemDefaults.colors(
                    selectedContainerColor = Space.Cyan.copy(alpha = 0.14f),
                    unselectedContainerColor = Color.Transparent,
                    selectedTextColor = Space.Cyan,
                    selectedIconColor = Space.Cyan,
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
                    IconButton(onClick = { sync.request() }) { Icon(Icons.Filled.Refresh, contentDescription = "Sync now", tint = Space.Cyan) }
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
                                Icon(Icons.Filled.Menu, contentDescription = "Chats", tint = Space.Cyan)
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
                                Icon(Icons.Filled.Add, contentDescription = "New chat", tint = Space.Cyan)
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
                        HorizontalDivider(color = Space.Cyan.copy(alpha = 0.18f))
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
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            OutlinedTextField(
                                value = engine.draft,
                                onValueChange = { engine.draft = it },
                                modifier = Modifier.weight(1f),
                                placeholder = { Text("Message $name…", color = Space.Muted) },
                                maxLines = 5,
                                shape = RoundedCornerShape(24.dp),
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = Space.Cyan.copy(alpha = 0.8f),
                                    unfocusedBorderColor = Space.Line,
                                    focusedContainerColor = Space.Panel.copy(alpha = 0.85f),
                                    unfocusedContainerColor = Space.Panel.copy(alpha = 0.7f),
                                    cursorColor = Space.Cyan,
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
                                    containerColor = Space.Cyan,
                                    contentColor = Color(0xFF00262B),
                                )
                                when {
                                    engine.busy -> FilledIconButton(onClick = { engine.stop() }, colors = glow) {
                                        Icon(Icons.Filled.Close, contentDescription = "Stop")
                                    }
                                    capturing -> FilledIconButton(onClick = { voice.cancel() }, colors = glow) {
                                        Icon(Icons.Filled.Close, contentDescription = "Cancel listening")
                                    }
                                    engine.draft.isBlank() -> FilledIconButton(onClick = {
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
                        items(engine.messages, key = { it.id }) { msg -> MessageRow(msg, name) }
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
    val color = if (online) Space.Cyan else Space.Muted
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
            .background(Space.Cyan.copy(alpha = 0.22f), CircleShape)
    )
}

@Composable
private fun EmptyState(modifier: Modifier, app: App, name: String) {
    Box(modifier.fillMaxSize().padding(32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("✦", fontSize = 44.sp, color = Space.Cyan)
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

@Composable
private fun MessageRow(msg: UiMessage, name: String) {
    when (msg.role) {
        Role.TOOL -> Text(
            "› ${msg.text}",
            style = HudLabel.copy(letterSpacing = 0.5.sp),
            color = Space.Gold,
            modifier = Modifier.padding(horizontal = 10.dp),
        )
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
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                Box(
                    Modifier
                        .widthIn(max = 320.dp)
                        .background(
                            Brush.linearGradient(listOf(Space.Cyan.copy(alpha = 0.30f), Space.Violet.copy(alpha = 0.30f))),
                            shape,
                        )
                        .border(1.dp, Space.Cyan.copy(alpha = 0.45f), shape)
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                ) {
                    SelectionContainer { Text(msg.text, color = Space.Text) }
                }
            }
        }
        Role.ASSISTANT -> {
            val shape = RoundedCornerShape(18.dp, 18.dp, 18.dp, 4.dp)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
                Column(
                    Modifier
                        .widthIn(max = 320.dp)
                        .background(Space.Panel.copy(alpha = 0.82f), shape)
                        .border(1.dp, Space.Cyan.copy(alpha = 0.22f), shape)
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                ) {
                    Text(
                        "✦ ${name.uppercase()}",
                        style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 2.sp),
                        color = Space.Cyan.copy(alpha = 0.8f),
                    )
                    Spacer(Modifier.size(4.dp))
                    if (msg.reasoning.isNotBlank()) ReasoningBlock(msg.reasoning)
                    if (msg.text.isEmpty()) {
                        CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = Space.Cyan)
                    } else {
                        SelectionContainer { Text(msg.text, color = Space.Text) }
                    }
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
        color = Space.Violet.copy(alpha = 0.85f),
        modifier = Modifier
            .clickable { expanded = !expanded }
            .padding(bottom = 6.dp),
    )
}
