package com.stellai.app

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/**
 * One project: its name, colour and instructions, pinned notes, files and chats.
 * [projectId] is "new" to create one.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProjectScreen(
    projectId: String,
    onDone: () -> Unit,
    onCreated: (String) -> Unit,
    onOpenChat: (String) -> Unit,
    onNewChat: (String) -> Unit,
) {
    val app = App.instance
    val pm = app.projects
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val isNew = projectId == "new"
    val project = pm.get(projectId)
    val name = app.settings.assistantName.ifBlank { "Stella" }

    var title by rememberSaveable(projectId) { mutableStateOf(project?.name ?: "") }
    var color by rememberSaveable(projectId) { mutableStateOf(project?.color ?: app.settings.accentColor) }
    var instructions by rememberSaveable(projectId) { mutableStateOf(project?.instructions ?: "") }
    var newNote by rememberSaveable(projectId) { mutableStateOf("") }
    var saving by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }
    var viewing by remember { mutableStateOf<ProjectFile?>(null) }

    // Pick up edits made on other devices (only while the form hasn't been changed here)
    LaunchedEffect(projectId) {
        if (!isNew) {
            pm.refresh()
            pm.get(projectId)?.let {
                title = it.name; color = it.color; instructions = it.instructions
            }
        }
    }

    val pickFiles = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty() && project != null) scope.launch { pm.addFiles(context, project.id, uris) }
    }

    val changed = isNew || project == null ||
        title.trim() != project.name || color != project.color || instructions.trim() != project.instructions.trim()

    fun save() {
        if (title.isBlank() || saving) return
        saving = true
        scope.launch {
            val id = pm.save(if (isNew) null else projectId, title, color, instructions)
            saving = false
            if (id != null && isNew) onCreated(id)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        if (isNew) "New project" else (project?.name ?: "Project"),
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                },
                navigationIcon = {
                    IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
                },
                actions = {
                    if (!isNew) {
                        IconButton(onClick = { scope.launch { pm.refresh() } }) {
                            Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = Space.Accent)
                        }
                    }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding).imePadding(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (!isNew && project == null) {
                item {
                    Text(
                        if (pm.status.isNotEmpty()) pm.status else "This project no longer exists.",
                        color = Space.Muted,
                    )
                }
                return@LazyColumn
            }

            // ---- About -------------------------------------------------------------------
            item { Section("Project") }
            item {
                OutlinedTextField(
                    value = title,
                    onValueChange = { title = it.take(60) },
                    label = { Text("Name") },
                    placeholder = { Text("e.g. Power supply PCB") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            item {
                Row(
                    Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    ACCENTS.forEach { a ->
                        Box(
                            Modifier
                                .size(30.dp)
                                .background(a.main, CircleShape)
                                .border(if (a.id == color) 3.dp else 0.dp, Space.Text, CircleShape)
                                .clickable { color = a.id },
                        )
                    }
                }
            }
            item {
                OutlinedTextField(
                    value = instructions,
                    onValueChange = { instructions = it.take(6000) },
                    label = { Text("Instructions for $name") },
                    placeholder = { Text("e.g. 3.3 V logic, KiCad 9, 0805 parts. Prefer MicroPython.") },
                    minLines = 3,
                    maxLines = 10,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Button(onClick = { save() }, enabled = changed && title.isNotBlank() && !saving) {
                        Text(if (isNew) "Create project" else "Save changes")
                    }
                    if (saving) {
                        Spacer(Modifier.size(12.dp))
                        CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp, color = Space.Accent)
                    }
                }
            }
            if (pm.status.isNotEmpty()) {
                item { Text(pm.status, color = Space.Red, style = MaterialTheme.typography.bodySmall) }
            }
            if (isNew || project == null) return@LazyColumn

            // ---- Chats -------------------------------------------------------------------
            item { Section("Chats") }
            item {
                OutlinedButton(onClick = { onNewChat(project.id) }) {
                    Icon(Icons.Filled.Add, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.size(6.dp))
                    Text("New chat in this project")
                }
            }
            val chats = app.engine.conversations.filter { it.projectId == project.id }
            if (chats.isEmpty()) {
                item { Hint("No chats yet. You can also move a chat here from its ⋮ menu.") }
            }
            items(chats, key = { "c-" + it.id }) { c ->
                Card(Modifier.clickable { onOpenChat(c.id) }) {
                    Text(c.title.ifBlank { "Untitled" }, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.padding(vertical = 8.dp))
                }
            }

            // ---- Notes -------------------------------------------------------------------
            item { Section("Notes") }
            item {
                Hint("Short facts $name should always know in this project. She can add notes too (\"note that we chose the TPS5430\").")
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(
                        value = newNote,
                        onValueChange = { newNote = it.take(1000) },
                        placeholder = { Text("e.g. Input is 12–24 V, output 5 V 3 A") },
                        modifier = Modifier.weight(1f),
                        maxLines = 4,
                    )
                    Spacer(Modifier.size(8.dp))
                    Button(
                        onClick = { val t = newNote; newNote = ""; scope.launch { pm.addNote(project.id, t) } },
                        enabled = newNote.isNotBlank(),
                    ) { Text("Add") }
                }
            }
            items(project.notes, key = { "n-" + it.id }) { n ->
                Card {
                    Column(Modifier.weight(1f).padding(vertical = 6.dp)) {
                        Text(n.text)
                        if (n.source == "stella") {
                            Text("Saved by $name", style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 0.5.sp), color = Space.Muted)
                        }
                    }
                    IconButton(onClick = { scope.launch { pm.deleteNote(project.id, n.id) } }) {
                        Icon(Icons.Filled.Delete, contentDescription = "Delete note", tint = Space.Muted)
                    }
                }
            }

            // ---- Files -------------------------------------------------------------------
            item { Section("Files") }
            item {
                Hint(
                    "Datasheets (PDF), code, text, Word documents and images (described by the vision add-on). " +
                        "Small files are given to $name whole; for big ones she gets the parts related to your question " +
                        "and can search the rest."
                )
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(onClick = { pickFiles.launch(arrayOf("*/*")) }, enabled = pm.uploading == null) {
                        Icon(Icons.Filled.Add, contentDescription = null, modifier = Modifier.size(18.dp))
                        Spacer(Modifier.size(6.dp))
                        Text("Add files")
                    }
                    pm.uploading?.let {
                        Spacer(Modifier.size(12.dp))
                        CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = Space.Accent)
                        Spacer(Modifier.size(8.dp))
                        Text("Reading $it…", color = Space.Muted, style = MaterialTheme.typography.bodySmall,
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
            items(project.files, key = { "f-" + it.id }) { f ->
                Card(Modifier.clickable { viewing = f }) {
                    Text(
                        when (f.kind) {
                            "pdf" -> "📄"
                            "image" -> "🖼"
                            "code" -> "⌨"
                            else -> "📝"
                        },
                        modifier = Modifier.padding(end = 10.dp),
                    )
                    Column(Modifier.weight(1f).padding(vertical = 6.dp)) {
                        Text(f.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        Text(
                            "${fileSize(f.size)} · ~${f.tokens} tokens" + if (f.note.isNotBlank()) " · ${f.note}" else "",
                            style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 0.5.sp),
                            color = if (f.note.isNotBlank()) Space.Gold else Space.Muted,
                            maxLines = 2,
                        )
                    }
                    IconButton(onClick = { scope.launch { pm.deleteFile(project.id, f.id) } }) {
                        Icon(Icons.Filled.Delete, contentDescription = "Remove file", tint = Space.Muted)
                    }
                }
            }

            // ---- Danger zone --------------------------------------------------------------
            item { Spacer(Modifier.size(12.dp)) }
            item {
                TextButton(onClick = { confirmDelete = true }) { Text("Delete project", color = Space.Red) }
            }
        }
    }

    if (confirmDelete && project != null) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            containerColor = Space.Panel,
            title = { Text("Delete “${project.name}”?") },
            text = { Text("Its notes and files are deleted on the server. Its chats are kept and move out of the project.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    scope.launch { pm.delete(project.id); onDone() }
                }) { Text("Delete", color = Space.Red) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancel") } },
        )
    }

    viewing?.let { f ->
        var text by remember(f.id) { mutableStateOf<String?>(null) }
        LaunchedEffect(f.id) {
            text = try {
                pm.fileText(project?.id ?: "", f.id).let { if (it.length > 20000) it.take(20000) + "\n\n[…]" else it }
            } catch (e: Exception) {
                "Couldn't load: ${app.engine.friendlyError(e)}"
            }
        }
        AlertDialog(
            onDismissRequest = { viewing = null },
            containerColor = Space.Panel,
            title = { Text(f.name, maxLines = 2, overflow = TextOverflow.Ellipsis) },
            text = {
                Column(Modifier.heightIn(max = 460.dp).verticalScroll(rememberScrollState())) {
                    Text("What $name reads from this file:", style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                    Spacer(Modifier.size(8.dp))
                    val t = text
                    if (t == null) {
                        CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp, color = Space.Accent)
                    } else {
                        SelectionContainer {
                            Text(t, fontFamily = FontFamily.Monospace, fontSize = 12.sp, color = Space.Text)
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { viewing = null }) { Text("Close") } },
        )
    }
}

@Composable
private fun Section(text: String) {
    Text(
        text.uppercase(),
        style = HudLabel.copy(fontSize = 12.sp, letterSpacing = 2.sp),
        color = Space.Accent,
        modifier = Modifier.padding(top = 10.dp),
    )
}

@Composable
private fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = Space.Muted)
}

@Composable
private fun Card(modifier: Modifier = Modifier, content: @Composable androidx.compose.foundation.layout.RowScope.() -> Unit) {
    Row(
        modifier
            .fillMaxWidth()
            .border(1.dp, Space.Line, RoundedCornerShape(12.dp))
            .padding(start = 14.dp, end = 4.dp, top = 4.dp, bottom = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        content = content,
    )
}

private fun fileSize(bytes: Long): String = when {
    bytes >= 1_000_000 -> "%.1f MB".format(bytes / 1_000_000f)
    bytes >= 1_000 -> "${bytes / 1000} KB"
    else -> "$bytes B"
}
