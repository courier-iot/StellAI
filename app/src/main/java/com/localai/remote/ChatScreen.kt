package com.localai.remote

import android.Manifest
import android.text.format.DateUtils
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
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
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(onOpenSettings: () -> Unit) {
    val app = App.instance
    val engine = app.engine
    val voice = app.voice
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
            ModalDrawerSheet {
                Text("Chats", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(20.dp, 20.dp, 20.dp, 12.dp))
                NavigationDrawerItem(
                    label = { Text("New chat") },
                    icon = { Icon(Icons.Filled.Add, contentDescription = null) },
                    selected = false,
                    onClick = { engine.newChat(); closeDrawer() },
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                LazyColumn(Modifier.weight(1f)) {
                    items(engine.conversations, key = { it.id }) { c ->
                        NavigationDrawerItem(
                            label = { Text(c.title.ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                            selected = c.id == engine.currentId,
                            onClick = { engine.open(c.id); closeDrawer() },
                            badge = {
                                IconButton(onClick = { confirmDelete = c }, modifier = Modifier.size(32.dp)) {
                                    Icon(Icons.Filled.Delete, contentDescription = "Delete chat", modifier = Modifier.size(18.dp))
                                }
                            },
                            modifier = Modifier.padding(horizontal = 12.dp),
                        )
                    }
                }
                HorizontalDivider()
                Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    val sync = app.sync
                    val label = when {
                        sync.status.startsWith("Sync failed") -> sync.status
                        sync.lastSuccess > 0 -> "Synced " + DateUtils.getRelativeTimeSpanString(
                            sync.lastSuccess, System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS,
                        )
                        else -> sync.status
                    }
                    Text(label, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f), maxLines = 2)
                    IconButton(onClick = { sync.request() }) { Icon(Icons.Filled.Refresh, contentDescription = "Sync now") }
                }
            }
        },
    ) {
        Scaffold(
            topBar = {
                TopAppBar(
                    navigationIcon = {
                        IconButton(onClick = { scope.launch { drawerState.open() } }) {
                            Icon(Icons.Filled.Menu, contentDescription = "Chats")
                        }
                    },
                    title = {
                        Column {
                            Text(engine.currentTitle, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            val subtitle = when {
                                engine.status.isNotEmpty() -> engine.status
                                voice.phase == VoicePhase.LISTENING -> "🎙 Listening…"
                                voice.phase == VoicePhase.TRANSCRIBING -> "Understanding…"
                                app.assistantRunning -> "Say “${app.settings.wakeWordLabel}”"
                                else -> ""
                            }
                            if (subtitle.isNotEmpty()) {
                                Text(subtitle, style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    },
                    actions = {
                        IconButton(onClick = { engine.newChat() }) {
                            Icon(Icons.Filled.Add, contentDescription = "New chat")
                        }
                        IconButton(onClick = onOpenSettings) {
                            Icon(Icons.Filled.Settings, contentDescription = "Settings")
                        }
                    },
                )
            },
            bottomBar = {
                Surface(tonalElevation = 3.dp) {
                    Column(Modifier.windowInsetsPadding(WindowInsets.ime.union(WindowInsets.navigationBars))) {
                        voice.lastError?.let { err ->
                            Text(
                                "$err  (tap to dismiss)",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.error,
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
                                placeholder = { Text("Ask or tell it to do something…") },
                                maxLines = 5,
                                shape = RoundedCornerShape(24.dp),
                                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                                keyboardActions = KeyboardActions(onSend = { submit() }),
                            )
                            Spacer(Modifier.size(8.dp))
                            val capturing = voice.phase == VoicePhase.LISTENING || voice.phase == VoicePhase.TRANSCRIBING
                            when {
                                engine.busy -> FilledIconButton(onClick = { engine.stop() }) {
                                    Icon(Icons.Filled.Close, contentDescription = "Stop")
                                }
                                capturing -> FilledIconButton(onClick = { voice.cancel() }) {
                                    Icon(Icons.Filled.Close, contentDescription = "Cancel listening")
                                }
                                engine.draft.isBlank() -> FilledIconButton(onClick = {
                                    if (voice.hasMicPermission()) voice.listenNow()
                                    else micPermission.launch(Manifest.permission.RECORD_AUDIO)
                                }) {
                                    Text("🎙", style = MaterialTheme.typography.titleMedium)
                                }
                                else -> FilledIconButton(onClick = { submit() }) {
                                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
                                }
                            }
                        }
                    }
                }
            },
        ) { padding ->
            if (engine.messages.isEmpty()) {
                EmptyState(Modifier.padding(padding), app)
            } else {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxSize().padding(padding),
                    contentPadding = PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(engine.messages, key = { it.id }) { msg -> MessageRow(msg) }
                }
            }
        }
    }

    confirmDelete?.let { c ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text("Delete chat?") },
            text = { Text("“${c.title}” will be deleted on all your devices.") },
            confirmButton = {
                TextButton(onClick = { engine.delete(c.id); confirmDelete = null }) { Text("Delete") }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = null }) { Text("Cancel") }
            },
        )
    }
}

@Composable
private fun EmptyState(modifier: Modifier, app: App) {
    Box(modifier.fillMaxSize().padding(32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("Your laptop's AI, on your phone", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.size(12.dp))
            val wake = if (app.assistantRunning) "Say “${app.settings.wakeWordLabel}, set an alarm for 6:45”\nor tap 🎙\n\n" else "Tap 🎙 and speak, or type:\n"
            Text(
                wake + "“Start a 10 minute timer”\n“Open Spotify”\n“Turn on the flashlight”",
                textAlign = TextAlign.Center,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun MessageRow(msg: UiMessage) {
    val colors = MaterialTheme.colorScheme
    when (msg.role) {
        Role.TOOL -> Text(
            msg.text,
            style = MaterialTheme.typography.labelLarge,
            color = colors.tertiary,
            modifier = Modifier.padding(horizontal = 8.dp),
        )
        Role.ERROR -> Text(
            msg.text,
            color = colors.onErrorContainer,
            modifier = Modifier
                .fillMaxWidth()
                .background(colors.errorContainer, RoundedCornerShape(12.dp))
                .padding(12.dp),
        )
        Role.USER, Role.ASSISTANT -> {
            val isUser = msg.role == Role.USER
            Row(Modifier.fillMaxWidth(), horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start) {
                Column(
                    Modifier
                        .widthIn(max = 320.dp)
                        .background(
                            if (isUser) colors.primaryContainer else colors.surfaceVariant,
                            RoundedCornerShape(18.dp),
                        )
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                ) {
                    if (msg.reasoning.isNotBlank()) ReasoningBlock(msg.reasoning)
                    if (msg.text.isEmpty()) {
                        CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    } else {
                        SelectionContainer {
                            Text(
                                msg.text,
                                color = if (isUser) colors.onPrimaryContainer else colors.onSurfaceVariant,
                            )
                        }
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
        text = if (expanded) reasoning.trim() else "💭 Thought process (tap to show)",
        style = MaterialTheme.typography.bodySmall,
        fontStyle = FontStyle.Italic,
        color = MaterialTheme.colorScheme.outline,
        modifier = Modifier
            .clickable { expanded = !expanded }
            .padding(bottom = 6.dp),
    )
}
