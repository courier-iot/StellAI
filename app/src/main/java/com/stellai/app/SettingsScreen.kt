package com.stellai.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.LifecycleResumeEffect
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun SettingsScreen(onDone: () -> Unit) {
    val app = App.instance
    val s = app.settings
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var url by rememberSaveable { mutableStateOf(s.serverUrl) }
    var key by rememberSaveable { mutableStateOf(s.apiKey) }
    var showKey by rememberSaveable { mutableStateOf(false) }
    var thinking by rememberSaveable { mutableStateOf(s.thinking) }
    var name by rememberSaveable { mutableStateOf(s.assistantName) }
    var wake by rememberSaveable { mutableStateOf(s.wakeWordEnabled) }
    var wakeWord by rememberSaveable { mutableStateOf(s.wakeWord) }
    var sensitivity by rememberSaveable { mutableStateOf(s.wakeSensitivity) }
    var speak by rememberSaveable { mutableStateOf(s.speakReplies) }
    var testResult by remember { mutableStateOf<String?>(null) }

    // Re-check permissions when returning from Android's settings pages.
    var overlayAllowed by remember { mutableStateOf(Settings.canDrawOverlays(context)) }
    var micAllowed by remember { mutableStateOf(app.voice.hasMicPermission()) }
    LifecycleResumeEffect(Unit) {
        overlayAllowed = Settings.canDrawOverlays(context)
        micAllowed = app.voice.hasMicPermission()
        onPauseOrDispose {}
    }

    val permissions = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        micAllowed = app.voice.hasMicPermission()
        if (result[Manifest.permission.RECORD_AUDIO] == false) wake = false
    }

    fun save() {
        s.serverUrl = AppSettings.normalizeUrl(url)
        s.apiKey = key.trim()
        s.thinking = thinking
        s.assistantName = name.trim().ifBlank { "Stella" }
        s.wakeWordEnabled = wake && micAllowed
        s.wakeWord = wakeWord
        s.wakeSensitivity = sensitivity
        s.speakReplies = speak
        (context as? Activity)?.let { app.applySettings(it) }
        onDone()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onDone) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                actions = { TextButton(onClick = { save() }) { Text("Save") } },
            )
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            // ---- Connection --------------------------------------------------------------
            SectionTitle("Server connection")
            Hint("On your server computer, open \"StellAI Server\". Its terminal shows the server address and API key. Use the Tailscale address to reach it from anywhere.")
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("Server address") },
                placeholder = { Text("http://100.101.102.103:8080") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = key,
                onValueChange = { key = it },
                label = { Text("API key") },
                singleLine = true,
                visualTransformation = if (showKey) VisualTransformation.None else PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                trailingIcon = {
                    TextButton(onClick = { showKey = !showKey }) { Text(if (showKey) "Hide" else "Show") }
                },
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedButton(onClick = {
                testResult = "Connecting…"
                scope.launch {
                    val server = AppSettings.normalizeUrl(url)
                    val ai = try {
                        val model = withContext(Dispatchers.IO) { LlamaClient().testConnection(server, key) }
                        "✓ AI connected — model: $model"
                    } catch (e: Exception) {
                        "✗ AI: ${app.engine.friendlyError(e)}"
                    }
                    val hub = if (withContext(Dispatchers.IO) { HubApi.health(server) }) {
                        "✓ Voice and sync service connected"
                    } else {
                        "✗ Voice/sync service not reachable (re-run the setup script on the server)"
                    }
                    testResult = "$ai\n$hub"
                }
            }) { Text("Test connection") }
            testResult?.let { Text(it) }

            HorizontalDivider()
            SectionTitle("Assistant")
            OutlinedTextField(
                value = name,
                onValueChange = { name = it.take(24) },
                label = { Text("Assistant name") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            SettingSwitch("Thinking mode", "Smarter on hard questions, but much slower without a GPU.", thinking) { thinking = it }

            HorizontalDivider()
            SectionTitle("Voice assistant")
            SettingSwitch(
                "Wake word",
                "Listen in the background for the wake word. Android shows a permanent notification, and it uses some battery.",
                wake,
            ) { on ->
                wake = on
                if (on && !micAllowed) {
                    val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
                    if (Build.VERSION.SDK_INT >= 33) perms.add(Manifest.permission.POST_NOTIFICATIONS)
                    permissions.launch(perms.toTypedArray())
                }
            }
            if (wake) {
                Text("Wake word", style = MaterialTheme.typography.labelLarge)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    AppSettings.WAKE_WORDS.forEach { (id, label) ->
                        FilterChip(selected = wakeWord == id, onClick = { wakeWord = id }, label = { Text(label) })
                    }
                }
                Text("Sensitivity", style = MaterialTheme.typography.labelLarge)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("Low" to 0, "Normal" to 1, "High" to 2).forEach { (label, v) ->
                        FilterChip(selected = sensitivity == v, onClick = { sensitivity = v }, label = { Text(label) })
                    }
                }
                Hint("Raise sensitivity if it misses you; lower it if it wakes up by itself.")
            }
            SettingSwitch("Speak replies", "Read answers aloud when you asked by voice.", speak) { speak = it }

            Text("Default assistant", style = MaterialTheme.typography.labelLarge)
            Hint("Make StellAI your phone's assistant, so long-pressing the power or home button starts listening. Choose \"StellAI\" under Digital assistant app.")
            OutlinedButton(onClick = {
                val intents = listOf(
                    Intent(Settings.ACTION_VOICE_INPUT_SETTINGS),
                    Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS),
                    Intent(Settings.ACTION_SETTINGS),
                )
                for (i in intents) {
                    try {
                        context.startActivity(i)
                        break
                    } catch (_: Exception) {
                    }
                }
            }) { Text("Open assistant settings") }

            Text("Background actions", style = MaterialTheme.typography.labelLarge)
            if (overlayAllowed) {
                Hint("✓ Allowed: voice commands can open apps, maps and the dialer even when the app isn't on screen.")
            } else {
                Hint("Android blocks apps from opening other apps from the background. Allow \"Display over other apps\" so voice commands can open apps, maps and the dialer.")
                OutlinedButton(onClick = {
                    try {
                        context.startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}")))
                    } catch (_: Exception) {
                    }
                }) { Text("Allow background actions") }
            }

            HorizontalDivider()
            SectionTitle("Sync")
            Hint("Chats are stored on the server and shared by every phone or tablet set up with the same server and key.")
            Text(app.sync.status, style = MaterialTheme.typography.bodyMedium)
            OutlinedButton(onClick = { app.sync.request() }) { Text("Sync now") }

            Button(onClick = { save() }, modifier = Modifier.fillMaxWidth()) { Text("Save") }
        }
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text("✦ " + text.uppercase(), style = HudLabel.copy(fontSize = 13.sp, letterSpacing = 2.5.sp), color = Space.Cyan)
}

@Composable
private fun Hint(text: String) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun SettingSwitch(title: String, detail: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(title)
            Text(
                detail,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
