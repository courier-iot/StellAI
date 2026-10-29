package com.stellai.app

import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/** Everything Stella remembers about you: view, add and delete. Stored on the server. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MemoryScreen(onDone: () -> Unit) {
    val app = App.instance
    val memory = app.memory
    val name = app.settings.assistantName.ifBlank { "Stella" }
    val scope = rememberCoroutineScope()
    var newFact by rememberSaveable { mutableStateOf("") }
    var loading by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        memory.refresh()
        loading = false
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Memory") },
                navigationIcon = {
                    IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
                },
                actions = {
                    IconButton(onClick = { scope.launch { loading = true; memory.refresh(); loading = false } }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = Space.Accent)
                    }
                },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).imePadding()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(
                    "$name uses these facts in every conversation, on all your devices. " +
                        "Delete anything that's wrong or private.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(
                        value = newFact,
                        onValueChange = { newFact = it.take(300) },
                        placeholder = { Text("e.g. I use KiCad for PCB design") },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                    )
                    Spacer(Modifier.size(8.dp))
                    Button(
                        onClick = {
                            val fact = newFact
                            newFact = ""
                            scope.launch { memory.add(fact) }
                        },
                        enabled = newFact.isNotBlank(),
                    ) { Text("Add") }
                }
                if (memory.status.isNotEmpty()) Text(memory.status, color = Space.Red, style = MaterialTheme.typography.bodySmall)
            }
            when {
                loading && memory.items.isEmpty() -> Text("Loading…", color = Space.Muted, modifier = Modifier.padding(16.dp))
                memory.items.isEmpty() -> Text(
                    "Nothing yet. Tell $name things like \"remember that I prefer metric units\", or turn on " +
                        "\"Learn from conversations\" in Settings.",
                    color = Space.Muted,
                    modifier = Modifier.padding(16.dp),
                )
                else -> LazyColumn(
                    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(memory.items, key = { it.id }) { m ->
                        Row(
                            Modifier
                                .fillMaxWidth()
                                .border(1.dp, Space.Line, RoundedCornerShape(12.dp))
                                .padding(start = 14.dp, top = 6.dp, bottom = 6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(m.text)
                                Text(
                                    when (m.source) {
                                        "learned" -> "Learned from a conversation"
                                        "stella" -> "Saved by $name"
                                        else -> "Added by you"
                                    },
                                    style = HudLabel.copy(fontSize = 10.sp, letterSpacing = 0.5.sp),
                                    color = Space.Muted,
                                )
                            }
                            IconButton(onClick = { scope.launch { memory.delete(m.id) } }) {
                                Icon(Icons.Filled.Delete, contentDescription = "Forget this", tint = Space.Muted)
                            }
                        }
                    }
                }
            }
        }
    }
}
