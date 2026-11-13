package com.stellai.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** One pinned note of a chat: a fact Stella always keeps in mind there and never summarizes away. */
data class ChatNote(val id: String, val text: String, val byStella: Boolean)

private fun parseNotes(o: JSONObject): List<ChatNote> {
    val arr = o.optJSONArray("notes") ?: return emptyList()
    return (0 until arr.length()).mapNotNull { i ->
        arr.optJSONObject(i)?.let { ChatNote(it.optString("id"), it.optString("text"), it.optString("source") == "stella") }
    }
}

/**
 * The chat's pinned notes (goals, decisions, names, story facts): always included when Stella answers in this chat,
 * never summarized away. She pins them herself when something is decided; here they can be added and removed.
 */
@Composable
fun PinnedNotesDialog(app: App, convId: String, name: String, onDismiss: () -> Unit) {
    var notes by remember { mutableStateOf<List<ChatNote>?>(null) }
    var error by remember { mutableStateOf("") }
    var draft by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    fun call(block: () -> JSONObject) {
        scope.launch {
            try {
                notes = parseNotes(withContext(Dispatchers.IO) { block() })
                error = ""
            } catch (e: Exception) {
                error = e.message.orEmpty().ifBlank { "Couldn't reach the server" }
            }
        }
    }

    LaunchedEffect(convId) { call { HubApi.chatNotes(app.settings.serverUrl, app.settings.apiKey, convId) } }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Space.Panel,
        title = { IconText(LineIcons.Pin, "Pinned notes", size = 20.dp) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(
                    "$name always keeps these in mind in this chat, however long it gets. She pins decisions, " +
                        "names and facts herself; add your own or remove ones that changed.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                )
                Column(
                    Modifier.heightIn(max = 320.dp).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    val list = notes
                    when {
                        list == null && error.isEmpty() -> Text("Loading…", color = Space.Muted)
                        list.isNullOrEmpty() && error.isEmpty() -> Text("Nothing pinned yet.", color = Space.Muted)
                    }
                    list?.forEach { n ->
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            if (n.byStella) {
                                IconText(LineIcons.Sparkle, n.text, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium, size = 14.dp)
                            } else {
                                Text("• " + n.text, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                            }
                            IconButton(onClick = {
                                call { HubApi.deleteChatNote(app.settings.serverUrl, app.settings.apiKey, convId, n.id) }
                            }) {
                                Icon(Icons.Filled.Close, contentDescription = "Unpin", tint = Space.Muted)
                            }
                        }
                    }
                }
                if (error.isNotEmpty()) Text(error, color = Space.Red, style = MaterialTheme.typography.bodySmall)
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    label = { Text("Pin a fact for this chat") },
                    singleLine = false,
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            TextButton(enabled = draft.isNotBlank(), onClick = {
                val text = draft.trim()
                draft = ""
                call { HubApi.addChatNote(app.settings.serverUrl, app.settings.apiKey, convId, text) }
            }) { Text("Pin") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Close") } },
    )
}
