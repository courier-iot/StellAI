package com.stellai.app

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** A document beside a chat, as the server sends it. */
data class ChatDoc(
    val id: String, val title: String, val kind: String, val language: String, val version: Int, val text: String,
    val versions: List<Triple<Int, String, String>>,   // version, who ("stella"/"user"), note
) {
    companion object {
        fun from(o: JSONObject): ChatDoc {
            val vs = o.optJSONArray("versions")
            return ChatDoc(
                o.optString("id"), o.optString("title"), o.optString("kind"), o.optString("language"),
                o.optInt("version"), o.optString("text"),
                (0 until (vs?.length() ?: 0)).mapNotNull { i ->
                    vs?.optJSONObject(i)?.let { Triple(it.optInt("version"), it.optString("by"), it.optString("note")) }
                },
            )
        }
    }
}

private val LANG_BY_EXT = mapOf(
    "py" to "python", "kt" to "kotlin", "js" to "javascript", "ts" to "typescript", "cpp" to "cpp", "c" to "c",
    "h" to "c", "ino" to "cpp", "java" to "java", "rs" to "rust", "go" to "go", "sh" to "bash",
)

/**
 * Documents beside the chat: long code, chapters and write-ups Stella keeps out of the chat itself. She edits them a
 * section at a time; every change is a version. You can read, copy, edit (refused if she changed it meanwhile),
 * go back to an earlier version, or start a new one.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DocsDialog(app: App, convId: String, name: String, focus: String?, changed: Int, onDismiss: () -> Unit) {
    val url = app.settings.serverUrl
    val key = app.settings.apiKey
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    var list by remember { mutableStateOf<List<Pair<String, String>>>(emptyList()) }   // id, title
    var cur by remember { mutableStateOf(focus) }
    var doc by remember { mutableStateOf<ChatDoc?>(null) }
    var loaded by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf(false) }
    var draft by remember { mutableStateOf("") }
    var error by remember { mutableStateOf("") }
    var versionsMenu by remember { mutableStateOf(false) }
    var newDialog by remember { mutableStateOf(false) }
    var confirmRevert by remember { mutableStateOf<Int?>(null) }

    fun load(id: String? = cur) {
        scope.launch {
            try {
                val docs = withContext(Dispatchers.IO) { HubApi.chatDocs(url, key, convId) }.optJSONArray("docs")
                list = (0 until (docs?.length() ?: 0)).mapNotNull { i ->
                    docs?.optJSONObject(i)?.let { it.optString("id") to it.optString("title") }
                }
                cur = id?.takeIf { want -> list.any { it.first == want } } ?: list.lastOrNull()?.first
                doc = cur?.let { c -> ChatDoc.from(withContext(Dispatchers.IO) { HubApi.doc(url, key, c) }) }
                error = ""
            } catch (e: Exception) {
                error = e.message.orEmpty().ifBlank { "Couldn't reach the server" }
            }
            loaded = true
        }
    }

    fun act(block: () -> JSONObject) {
        scope.launch {
            try {
                val d = ChatDoc.from(withContext(Dispatchers.IO) { block() })
                editing = false
                load(d.id)
            } catch (e: Exception) {
                val msg = e.message.orEmpty()
                error = if ("changed meanwhile" in msg)
                    "$name changed this document while you were editing. Copy your text, reopen it and add your change again."
                else msg.ifBlank { "Couldn't save" }
            }
        }
    }

    LaunchedEffect(convId, focus) { load(focus) }
    LaunchedEffect(changed) { if (loaded && !editing) load() }   // Stella edited it while it was open

    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize(), color = Space.Panel) {
            Column(Modifier.fillMaxSize().padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconText(LineIcons.Document, "Documents", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium, size = 20.dp)
                    IconButton(onClick = { newDialog = true }) { Icon(Icons.Filled.Add, "New document", tint = Space.Accent) }
                    IconButton(onClick = onDismiss) { Icon(Icons.Filled.Close, "Close", tint = Space.Muted) }
                }
                if (list.size > 1) {
                    Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        list.forEach { (id, title) ->
                            FilterChip(selected = id == cur, onClick = { editing = false; load(id) }, label = { Text(title) })
                        }
                    }
                }
                val d = doc
                Box(Modifier.weight(1f).fillMaxWidth().padding(vertical = 8.dp)) {
                    when {
                        d == null -> Text(
                            if (!loaded) "Loading…" else
                                "No documents in this chat yet. Ask $name to write something long " +
                                    "(“draft chapter 1 in a document”) or press ＋.",
                            color = Space.Muted,
                        )
                        editing -> OutlinedTextField(
                            value = draft, onValueChange = { draft = it }, modifier = Modifier.fillMaxSize(),
                            textStyle = TextStyle(fontFamily = FontFamily.Monospace, fontSize = 13.sp, color = Space.Text),
                        )
                        d.kind == "code" -> SelectionContainer {
                            Text(
                                d.text, color = Space.Text,
                                style = TextStyle(fontFamily = FontFamily.Monospace, fontSize = 12.sp),
                                modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState())
                                    .horizontalScroll(rememberScrollState()),
                            )
                        }
                        else -> Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
                            SelectionContainer { MarkdownContent(d.text, Space.Text) }
                        }
                    }
                }
                if (error.isNotEmpty()) Text(error, color = Space.Red, style = MaterialTheme.typography.bodySmall)
                if (d != null) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("v${d.version} · ${d.text.lines().size} lines", color = Space.Muted,
                            style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
                        TextButton(onClick = { clipboard.setText(AnnotatedString(if (editing) draft else d.text)) }) { Text("Copy") }
                        Box {
                            TextButton(enabled = !editing && d.versions.size > 1, onClick = { versionsMenu = true }) { Text("Versions") }
                            DropdownMenu(expanded = versionsMenu, onDismissRequest = { versionsMenu = false }) {
                                d.versions.filter { it.first != d.version }.forEach { (v, by, note) ->
                                    DropdownMenuItem(
                                        text = { Text("v$v · ${if (by == "user") "you" else name}" + if (note.isNotEmpty()) " · $note" else "") },
                                        onClick = { versionsMenu = false; confirmRevert = v },
                                    )
                                }
                            }
                        }
                        if (editing) {
                            TextButton(onClick = { editing = false }) { Text("Cancel") }
                            TextButton(onClick = { act { HubApi.saveDoc(url, key, d.id, draft, d.version) } }) { Text("Save") }
                        } else {
                            TextButton(onClick = { draft = d.text; editing = true; error = "" }) { Text("Edit") }
                        }
                    }
                }
            }
        }
    }

    confirmRevert?.let { v ->
        val d = doc
        AlertDialog(
            onDismissRequest = { confirmRevert = null },
            containerColor = Space.Panel,
            title = { Text("Go back to version $v?") },
            text = { Text("The current text stays in the version list.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmRevert = null
                    if (d != null) act { HubApi.revertDoc(url, key, d.id, v) }
                }) { Text("Go back") }
            },
            dismissButton = { TextButton(onClick = { confirmRevert = null }) { Text("Cancel") } },
        )
    }

    if (newDialog) {
        var title by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { newDialog = false },
            containerColor = Space.Panel,
            title = { Text("New document") },
            text = {
                OutlinedTextField(value = title, onValueChange = { title = it }, singleLine = true,
                    label = { Text("Title (end it in .py, .kt, … for code)") })
            },
            confirmButton = {
                TextButton(enabled = title.isNotBlank(), onClick = {
                    newDialog = false
                    val t = title.trim()
                    val lang = LANG_BY_EXT[t.substringAfterLast('.', "").lowercase()].orEmpty()
                    scope.launch {
                        try {
                            val d = ChatDoc.from(withContext(Dispatchers.IO) { HubApi.createDoc(url, key, convId, t, lang) })
                            load(d.id)
                            draft = ""
                            editing = true
                        } catch (e: Exception) {
                            error = e.message.orEmpty().ifBlank { "Couldn't create it" }
                        }
                    }
                }) { Text("Create") }
            },
            dismissButton = { TextButton(onClick = { newDialog = false }) { Text("Cancel") } },
        )
    }
}
