package com.stellai.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Thumbs up/down on Stella's replies (long-press a reply). A thumbs-down, with what was wrong, becomes a test case
 * her answers must pass and a one-line lesson she reads next time; a thumbs-up keeps a good answer working.
 */
object Ratings {
    /** Rates a reply on the server; returns a short message for the user. */
    suspend fun rate(messageId: String, rating: Int, note: String): String {
        val s = App.instance.settings
        return try {
            withContext(Dispatchers.IO) { HubApi.rate(s.serverUrl, s.apiKey, messageId, rating, note.trim()) }
            when {
                rating > 0 -> "Kept as a good answer in her test set"
                note.isNotBlank() -> "Thanks: added to her test set, and she'll learn from it"
                else -> "Thanks: added to her test set"
            }
        } catch (e: Exception) {
            "Couldn't save the rating: " + friendlyErrorText(e, s.serverUrl)
        }
    }
}

@Composable
fun RateDialog(onDismiss: () -> Unit, onSave: (String) -> Unit) {
    var note by remember { mutableStateOf("") }
    val name = App.instance.settings.assistantName.ifBlank { "Stella" }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("What was wrong?") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Optional, but it's how $name learns: it becomes a lesson she reads next time, and a test her " +
                    "answers must pass.", style = MaterialTheme.typography.bodySmall)
                OutlinedTextField(note, { note = it.take(600) }, placeholder = { Text("e.g. M3 clearance holes are 3.2 mm") },
                    minLines = 2, modifier = Modifier.fillMaxWidth())
            }
        },
        confirmButton = { TextButton(onClick = { onSave(note) }) { Text("Save") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
