package com.stellai.app

import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/** Lists the modes; your own ones can be added, edited and deleted. Built-in ones can be copied and changed. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ModesScreen(onDone: () -> Unit) {
    val app = App.instance
    val modes = app.modes
    val scope = rememberCoroutineScope()
    var editing by remember { mutableStateOf<Mode?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirmDelete by remember { mutableStateOf<Mode?>(null) }

    LaunchedEffect(Unit) { error = modes.refresh()?.let { "Couldn't reach the server: $it" } }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Modes") },
                navigationIcon = {
                    IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            item {
                Text(
                    "A mode is a mindset for a chat: its own instructions, creativity, thinking and tools. " +
                        "Pick one from the chip under the top bar; each chat remembers its mode on all your devices.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                )
            }
            error?.let { e -> item { Text(e, color = Space.Red, style = MaterialTheme.typography.bodySmall) } }
            item {
                OutlinedButton(onClick = { editing = Mode("", "", "★", "", "", builtIn = false) }) {
                    Icon(Icons.Filled.Add, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.size(6.dp))
                    Text("New mode")
                }
            }
            if (modes.custom.isNotEmpty()) {
                item { ModesHeader("Your modes") }
                items(modes.custom, key = { it.id }) { m ->
                    ModeRow(m) {
                        IconButton(onClick = { editing = m }) { Icon(Icons.Filled.Edit, contentDescription = "Edit", tint = Space.Muted) }
                        IconButton(onClick = { confirmDelete = m }) {
                            Icon(Icons.Filled.Delete, contentDescription = "Delete", tint = Space.Muted)
                        }
                    }
                }
            }
            item { ModesHeader("Built in") }
            items(BUILT_IN_MODES, key = { it.id }) { m ->
                ModeRow(m) {
                    if (m.id != GENERAL_MODE.id) {
                        TextButton(onClick = {
                            editing = m.copy(id = "", name = "My ${m.name}", builtIn = false)
                        }) { Text("Copy") }
                    }
                }
            }
        }
    }

    editing?.let { m ->
        ModeEditor(
            initial = m,
            onDismiss = { editing = null },
            onSave = { saved ->
                scope.launch {
                    val err = modes.save(saved)
                    if (err == null) editing = null else error = "Couldn't save: $err"
                }
            },
        )
    }

    confirmDelete?.let { m ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            containerColor = Space.Panel,
            title = { Text("Delete “${m.name}”?") },
            text = { Text("Chats using it switch back to General.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = null
                    scope.launch { error = modes.delete(m.id)?.let { "Couldn't delete: $it" } }
                }) { Text("Delete", color = Space.Red) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = null }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun ModesHeader(text: String) {
    Text(
        text.uppercase(),
        style = HudLabel.copy(fontSize = 12.sp, letterSpacing = 2.sp),
        color = Space.Accent,
        modifier = Modifier.padding(top = 10.dp),
    )
}

@Composable
private fun ModeRow(m: Mode, actions: @Composable () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .border(1.dp, Space.Line, RoundedCornerShape(12.dp))
            .padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(m.icon, fontSize = 20.sp, modifier = Modifier.widthIn(min = 34.dp))
        Column(Modifier.weight(1f)) {
            Text(m.name)
            Text(summary(m), style = MaterialTheme.typography.bodySmall, color = Space.Muted, maxLines = 2)
        }
        actions()
    }
}

private fun summary(m: Mode): String {
    val parts = mutableListOf<String>()
    if (m.builtIn) parts.add(m.description)
    parts.add(creativityLabel(m.temperature))
    if (m.thinking == "on") parts.add("thinks first")
    if (m.thinking == "off") parts.add("answers directly")
    if (m.tools == "minimal") parts.add("few tools")
    if (m.style == "story") parts.add("story memory")
    return parts.joinToString(" · ")
}

private fun creativityLabel(t: Double?): String = when {
    t == null -> "default creativity"
    t < 0.35 -> "precise"
    t < 0.7 -> "balanced"
    t < 1.0 -> "creative"
    else -> "very creative"
}

private val ICONS = listOf("★", "⌨", "✒", "⚡", "🎓", "💡", "📜", "🎮", "🍳", "🧪", "🎵", "📈", "🗺", "🛠", "❤", "🌙")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ModeEditor(initial: Mode, onDismiss: () -> Unit, onSave: (Mode) -> Unit) {
    val name = App.instance.settings.assistantName.ifBlank { "Stella" }
    var title by remember { mutableStateOf(initial.name) }
    var icon by remember { mutableStateOf(initial.icon) }
    var prompt by remember { mutableStateOf(initial.prompt) }
    var useDefaultTemp by remember { mutableStateOf(initial.temperature == null) }
    var temp by remember { mutableStateOf((initial.temperature ?: 0.7).toFloat()) }
    var thinking by remember { mutableStateOf(initial.thinking) }
    var minimal by remember { mutableStateOf(initial.tools == "minimal") }
    var story by remember { mutableStateOf(initial.style == "story") }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Space.Panel,
        title = { Text(if (initial.id.isBlank()) "New mode" else "Edit mode") },
        text = {
            Column(Modifier.heightIn(max = 520.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = title, onValueChange = { title = it.take(40) }, label = { Text("Name") },
                    placeholder = { Text("e.g. Game master") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    ICONS.forEach { i ->
                        Text(
                            i,
                            fontSize = 20.sp,
                            modifier = Modifier
                                .border(if (i == icon) 2.dp else 0.dp, Space.Accent, RoundedCornerShape(8.dp))
                                .clickable { icon = i }
                                .padding(6.dp),
                        )
                    }
                }
                OutlinedTextField(
                    value = prompt,
                    onValueChange = { prompt = it.take(6000) },
                    label = { Text("Instructions for $name") },
                    placeholder = { Text("How should $name think, write and behave in this mode?") },
                    minLines = 4,
                    maxLines = 12,
                    modifier = Modifier.fillMaxWidth(),
                )
                Text("Creativity: " + if (useDefaultTemp) "model default" else creativityLabel(temp.toDouble()) +
                    " (%.1f)".format(temp), color = Space.Text)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = useDefaultTemp, onCheckedChange = { useDefaultTemp = it })
                    Text("Use the default", style = MaterialTheme.typography.bodySmall)
                }
                if (!useDefaultTemp) {
                    Slider(value = temp, onValueChange = { temp = (it * 10).toInt() / 10f }, valueRange = 0f..1.5f)
                    Text("Low: precise and consistent (code, facts). High: varied and imaginative (stories, ideas).",
                        style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                }
                Text("Thinking before answering", color = Space.Text)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf("default" to "Settings", "on" to "Always", "off" to "Never").forEach { (v, label) ->
                        FilterChip(selected = thinking == v, onClick = { thinking = v }, label = { Text(label) })
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Fewer tools", color = Space.Text)
                        Text("No calculators or diagrams; keeps the focus on conversation.",
                            style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                    }
                    Switch(checked = minimal, onCheckedChange = { minimal = it })
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Story memory", color = Space.Text)
                        Text("When the chat's context refreshes, keep characters, plot and style instead of tasks.",
                            style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                    }
                    Switch(checked = story, onCheckedChange = { story = it })
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = title.isNotBlank(),
                onClick = {
                    onSave(
                        initial.copy(
                            name = title.trim(), icon = icon, prompt = prompt.trim(),
                            temperature = if (useDefaultTemp) null else temp.toDouble(),
                            thinking = thinking, tools = if (minimal) "minimal" else "all",
                            style = if (story) "story" else "work", builtIn = false,
                        )
                    )
                },
            ) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
