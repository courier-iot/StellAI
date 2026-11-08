package com.stellai.app

import androidx.compose.material.icons.outlined.Cancel
import androidx.compose.material.icons.outlined.WarningAmber
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
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
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.text.DateFormat
import java.util.Date

/** The StellAI server: what's running, switching models, a health check and backups. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ServerScreen(onDone: () -> Unit) {
    val app = App.instance
    val s = app.settings
    val scope = rememberCoroutineScope()
    var server by remember { mutableStateOf<JSONObject?>(null) }
    var backups by remember { mutableStateOf<JSONObject?>(null) }
    var doctor by remember { mutableStateOf<JSONObject?>(null) }
    var checking by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var confirmSwitch by remember { mutableStateOf<JSONObject?>(null) }
    var refreshTick by remember { mutableIntStateOf(0) }
    var copyTo by remember { mutableStateOf("") }
    var busyBackup by remember { mutableStateOf(false) }

    suspend fun load() {
        try {
            server = withContext(Dispatchers.IO) { HubApi.server(s.serverUrl, s.apiKey) }
            backups = withContext(Dispatchers.IO) { HubApi.backups(s.serverUrl, s.apiKey) }
            if (copyTo.isEmpty()) copyTo = backups?.optString("copy_to").orEmpty()
            error = null
        } catch (e: Exception) {
            error = "Couldn't reach the server: ${app.engine.friendlyError(e)}"
        }
    }

    fun act(block: suspend () -> String) {
        scope.launch {
            message = try {
                block()
            } catch (e: Exception) {
                app.engine.friendlyError(e).removePrefix("HTTP 400: ")
            }
            load()
            refreshTick++
        }
    }

    LaunchedEffect(Unit) { load() }
    // While something is happening (downloading, loading a model), keep the screen up to date
    LaunchedEffect(refreshTick) {
        repeat(90) {
            delay(2000)
            val models = server?.optJSONArray("models") ?: JSONArray()
            val downloading = (0 until models.length()).any {
                models.optJSONObject(it)?.optJSONObject("download")?.optString("state") == "downloading"
            }
            if (!downloading && server?.optBoolean("loaded") == true) return@LaunchedEffect
            load()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Server") },
                navigationIcon = { IconButton(onClick = onDone) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") } },
                actions = {
                    IconButton(onClick = { scope.launch { load() } }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = Space.Accent)
                    }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding).imePadding(),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            error?.let { e -> item { Text(e, color = Space.Red) } }
            message?.let { m -> item { Text(m, color = Space.Gold, style = MaterialTheme.typography.bodySmall) } }

            // ---- Status
            item { ServerHeader("Status") }
            item {
                val st = server
                Column(Modifier.fillMaxWidth().border(1.dp, Space.Line, RoundedCornerShape(12.dp)).padding(12.dp)) {
                    if (st == null) {
                        Text(if (error == null) "Loading…" else "Offline", color = Space.Muted)
                    } else {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            val on = st.optBoolean("loaded")
                            Box(Modifier.size(10.dp).background(if (on) Space.Accent else Space.Gold, CircleShape))
                            Spacer(Modifier.size(8.dp))
                            Text(if (on) "Running" else "Loading the model…")
                        }
                        Spacer(Modifier.size(6.dp))
                        val model = st.optString("model").ifBlank { st.optString("model_path") }
                        StatusLine("Model", model.ifBlank { "unknown" })
                        if (st.optInt("context") > 0) StatusLine("Context", "${st.optInt("context")} tokens")
                        if (st.optString("backend").isNotBlank()) StatusLine("Runs on", st.optString("backend").uppercase())
                        StatusLine("Memory", "${st.optInt("ram_gb")} GB RAM" +
                            if (st.optInt("vram_gb") > 0) " · ${st.optInt("vram_gb")} GB GPU" else "")
                        StatusLine("Up for", uptime(st.optLong("uptime_s")))
                    }
                }
            }

            // ---- Models
            val models = server?.optJSONArray("models") ?: JSONArray()
            if (models.length() > 0) {
                item { ServerHeader("Model") }
                item {
                    Text("Switching takes about a minute while the new model loads. Downloads continue on the server " +
                        "even if you close the app.", style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                }
                items((0 until models.length()).mapNotNull { models.optJSONObject(it) }, key = { it.optString("key") }) { m ->
                    ModelRow(
                        m,
                        onSwitch = { confirmSwitch = m },
                        onDownload = { act { withContext(Dispatchers.IO) { HubApi.downloadModel(s.serverUrl, s.apiKey, m.optString("key")) } } },
                    )
                }
            } else if (server != null) {
                item {
                    Text("This server can't switch models from the app yet (re-run its setup to enable it).",
                        style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                }
            }

            // ---- Health check
            item { ServerHeader("Health check") }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(enabled = !checking, onClick = {
                        checking = true
                        scope.launch {
                            doctor = try {
                                withContext(Dispatchers.IO) { HubApi.doctor(s.serverUrl, s.apiKey) }
                            } catch (e: Exception) {
                                error = app.engine.friendlyError(e)
                                null
                            }
                            checking = false
                        }
                    }) { Text("Check everything") }
                    if (checking) {
                        Spacer(Modifier.size(12.dp))
                        CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = Space.Accent)
                    }
                }
            }
            doctor?.optJSONArray("checks")?.let { checks ->
                items((0 until checks.length()).mapNotNull { checks.optJSONObject(it) }, key = { "d-" + it.optString("name") }) { c ->
                    CheckRow(c)
                }
            }

            // ---- Backups
            item { ServerHeader("Backups") }
            item {
                val b = backups
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(
                        "Every night the server saves your chats, memories, projects, photos and wake words. " +
                            "It keeps the last ${b?.optInt("keep") ?: 14}.",
                        style = MaterialTheme.typography.bodySmall, color = Space.Muted,
                    )
                    val last = b?.optLong("last") ?: 0
                    Text(if (last > 0) "Last backup: ${DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(last))}"
                        else "No backup yet")
                    b?.optString("error")?.takeIf { it.isNotBlank() }?.let { IconText(Icons.Outlined.WarningAmber, it, color = Space.Red) }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Button(enabled = !busyBackup, onClick = {
                            busyBackup = true
                            act {
                                try {
                                    val r = withContext(Dispatchers.IO) { HubApi.backupNow(s.serverUrl, s.apiKey) }
                                    "Saved ${r.optString("file")}"
                                } finally {
                                    busyBackup = false
                                }
                            }
                        }) { Text("Back up now") }
                        if (busyBackup) {
                            Spacer(Modifier.size(12.dp))
                            CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = Space.Accent)
                        }
                    }
                    OutlinedTextField(
                        value = copyTo, onValueChange = { copyTo = it }, singleLine = true,
                        label = { Text("Also copy to (folder on the server, e.g. a USB drive)") },
                        placeholder = { Text("/media/you/USB/stellai") }, modifier = Modifier.fillMaxWidth(),
                    )
                    TextButton(onClick = {
                        act {
                            withContext(Dispatchers.IO) { HubApi.backupConfig(s.serverUrl, s.apiKey, copyTo.trim(), b?.optInt("keep") ?: 14) }
                            if (copyTo.isBlank()) "Backups stay on the server only" else "Backups will also be copied to $copyTo"
                        }
                    }) { Text("Save backup folder") }
                    Text(
                        "To restore: Linux: run stellai-restore.sh on the server. Windows: StellAI app → Options → Restore a backup.",
                        style = MaterialTheme.typography.bodySmall, color = Space.Muted,
                    )
                }
            }
            val list = backups?.optJSONArray("backups") ?: JSONArray()
            items((0 until minOf(list.length(), 14)).mapNotNull { list.optJSONObject(it) }, key = { "b-" + it.optString("name") }) { f ->
                Text("${f.optString("name")} · ${f.optLong("size") / 1_000_000} MB", style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted)
            }
        }
    }

    confirmSwitch?.let { m ->
        AlertDialog(
            onDismissRequest = { confirmSwitch = null },
            containerColor = Space.Panel,
            title = { Text("Switch to ${m.optString("label")}?") },
            text = { Text("Stella is unavailable for about a minute while it loads. Chats, memories and settings stay the same.") },
            confirmButton = {
                TextButton(onClick = {
                    confirmSwitch = null
                    act { withContext(Dispatchers.IO) { HubApi.switchModel(s.serverUrl, s.apiKey, m.optString("key")) } }
                }) { Text("Switch") }
            },
            dismissButton = { TextButton(onClick = { confirmSwitch = null }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun ServerHeader(text: String) {
    Text(text.uppercase(), style = HudLabel.copy(fontSize = 12.sp, letterSpacing = 2.sp), color = Space.Accent,
        modifier = Modifier.padding(top = 10.dp))
}

@Composable
private fun StatusLine(label: String, value: String) {
    Row {
        Text(label, color = Space.Muted, modifier = Modifier.size(width = 90.dp, height = 22.dp))
        Text(value)
    }
}

@Composable
private fun ModelRow(m: JSONObject, onSwitch: () -> Unit, onDownload: () -> Unit) {
    val current = m.optBoolean("current")
    val dl = m.optJSONObject("download")
    Column(
        Modifier.fillMaxWidth()
            .border(1.dp, if (current) Space.Accent else Space.Line, RoundedCornerShape(12.dp))
            .padding(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(m.optString("label") + if (current) "  · in use" else "", color = if (current) Space.Accent else Space.Text)
                Text("${m.optString("about")} ${m.optDouble("size_gb")} GB, ${m.optInt("context") / 1024}K context.",
                    style = MaterialTheme.typography.bodySmall, color = Space.Muted)
            }
            when {
                current -> {}
                m.optBoolean("installed") -> TextButton(onClick = onSwitch) { Text("Switch") }
                dl?.optString("state") == "downloading" -> {}
                else -> TextButton(onClick = onDownload) { Text("Download") }
            }
        }
        if (dl != null) {
            when (dl.optString("state")) {
                "downloading" -> {
                    val total = dl.optLong("total")
                    val f = if (total > 0) dl.optLong("done").toFloat() / total else 0f
                    Spacer(Modifier.size(6.dp))
                    LinearProgressIndicator(progress = { f }, modifier = Modifier.fillMaxWidth(), color = Space.Accent, trackColor = Space.Line)
                    Text("Downloading… ${(f * 100).toInt()}%", style = MaterialTheme.typography.bodySmall, color = Space.Muted)
                }
                "failed" -> Text("Download failed: ${dl.optString("error")}", color = Space.Red, style = MaterialTheme.typography.bodySmall)
                "ready" -> if (!m.optBoolean("current")) Text("Downloaded; tap Switch to use it.", color = Space.Gold,
                    style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun CheckRow(c: JSONObject) {
    val state = c.optString("state")
    val (icon, color) = when (state) {
        "ok" -> Icons.Outlined.CheckCircle to Space.Accent
        "warn" -> Icons.Outlined.WarningAmber to Space.Gold
        else -> Icons.Outlined.Cancel to Space.Red
    }
    Row(Modifier.fillMaxWidth()) {
        Icon(icon, contentDescription = state, tint = color, modifier = Modifier.padding(end = 8.dp, top = 1.dp).size(18.dp))
        Column(Modifier.weight(1f)) {
            Text("${c.optString("name")}: ${c.optString("detail")}", color = if (state == "ok") Space.Text else color)
            val fix = c.optString("fix")
            if (fix.isNotBlank() && state != "ok") Text("→ $fix", style = MaterialTheme.typography.bodySmall, color = Space.Muted)
        }
    }
}

private fun uptime(sec: Long): String = when {
    sec < 3600 -> "${sec / 60} min"
    sec < 86400 -> "${sec / 3600} h ${(sec % 3600) / 60} min"
    else -> "${sec / 86400} days"
}

