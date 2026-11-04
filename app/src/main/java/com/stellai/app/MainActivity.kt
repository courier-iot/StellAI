package com.stellai.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

class MainActivity : ComponentActivity() {

    private val app get() = application as App

    /** Screen asked for by a notification (e.g. "tasks"). */
    private var requestedScreen by mutableStateOf<String?>(null)

    /** Server address + key from a pairing QR code, waiting for the user to confirm. */
    private var pendingPair by mutableStateOf<Pair<String, String>?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Space theme is always dark: light status/navigation bar icons.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        if (savedInstanceState == null) handleIntent(intent)
        setContent {
            StellarTheme {
                // Open settings first if the app hasn't been set up yet.
                // "chat", "settings", "memory", or "project:<id>" ("project:new" to create one)
                var screen by rememberSaveable { mutableStateOf(if (app.settings.apiKey.isBlank()) "settings" else "chat") }
                // A reminder notification asks for the reminders screen
                LaunchedEffect(requestedScreen) {
                    requestedScreen?.let { s ->
                        if (app.settings.apiKey.isNotBlank()) screen = s
                        requestedScreen = null
                    }
                }
                when (screen) {
                    "settings" -> {
                        BackHandler { screen = "chat" }
                        SettingsScreen(onDone = { screen = "chat" }, onOpenMemory = { screen = "memory" })
                    }
                    "tasks" -> {
                        BackHandler { screen = "chat" }
                        TasksScreen(onDone = { screen = "chat" })
                    }
                    "modes" -> {
                        BackHandler { screen = "chat" }
                        ModesScreen(onDone = { screen = "chat" })
                    }
                    "server" -> {
                        BackHandler { screen = "chat" }
                        ServerScreen(onDone = { screen = "chat" })
                    }
                    "memory" -> {
                        BackHandler { screen = "settings" }
                        MemoryScreen(onDone = { screen = "settings" })
                    }
                    else -> if (screen.startsWith("project:")) {
                        BackHandler { screen = "chat" }
                        ProjectScreen(
                            projectId = screen.removePrefix("project:"),
                            onDone = { screen = "chat" },
                            onCreated = { id -> screen = "project:$id" },
                            onOpenChat = { id -> app.engine.open(id); screen = "chat" },
                            onNewChat = { id -> app.engine.newChat(id); screen = "chat" },
                        )
                    } else {
                        ChatScreen(
                            onOpenSettings = { screen = "settings" },
                            onOpenProject = { id -> screen = "project:$id" },
                            onOpenModes = { screen = "modes" },
                            onOpenTasks = { screen = "tasks" },
                            onOpenServer = { screen = "server" },
                        )
                    }
                }
                pendingPair?.let { (url, key) ->
                    PairDialog(
                        url = url,
                        replacing = app.settings.serverUrl.isNotBlank() && app.settings.serverUrl != url,
                        onConfirm = {
                            app.settings.serverUrl = url
                            app.settings.apiKey = key
                            pendingPair = null
                            app.applySettings(this@MainActivity)
                            screen = "chat"
                        },
                        onDismiss = { pendingPair = null },
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onStart() {
        super.onStart()
        app.setAppVisible(true)
        // Restarts wake word listening after a reboot (Android only allows that from the foreground).
        app.applySettings(this)
    }

    override fun onStop() {
        app.setAppVisible(false)
        super.onStop()
    }

    /** Assistant gesture, headset voice button, or our own assist session: start listening. */
    private fun handleIntent(intent: Intent?) {
        intent ?: return
        intent.getStringExtra(EXTRA_SCREEN)?.let {
            requestedScreen = it
            intent.removeExtra(EXTRA_SCREEN)
        }
        val data = intent.data
        if (intent.action == Intent.ACTION_VIEW && data?.scheme == "stellai" && data.host == "pair") {
            val url = AppSettings.normalizeUrl(data.getQueryParameter("url").orEmpty())
            val key = data.getQueryParameter("key").orEmpty().trim()
            val validUrl = Regex("^https?://[A-Za-z0-9.:\\[\\]-]+(:\\d+)?$").matches(url)
            if (validUrl && key.length in 8..200 && key.none { it.isWhitespace() }) pendingPair = url to key
            setIntent(Intent(this, MainActivity::class.java))
            return
        }
        val listen = intent.getBooleanExtra(EXTRA_LISTEN, false) ||
            intent.action == Intent.ACTION_ASSIST ||
            intent.action == Intent.ACTION_VOICE_COMMAND
        if (!listen) return
        intent.removeExtra(EXTRA_LISTEN)
        if (app.voice.hasMicPermission() && app.settings.apiKey.isNotBlank()) app.voice.listenNow()
    }

    companion object {
        const val EXTRA_LISTEN = "com.stellai.app.LISTEN"
        const val EXTRA_SCREEN = "com.stellai.app.SCREEN"
    }
}

/** Asks before using a server from a pairing QR code: never trust a scanned link silently. */
@androidx.compose.runtime.Composable
private fun PairDialog(url: String, replacing: Boolean, onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Connect to this server?") },
        text = {
            Column {
                Text(url, color = Space.Accent)
                Spacer(Modifier.height(8.dp))
                Text(
                    if (replacing) "This replaces your current server and API key. Only connect to your own computer."
                    else "The address and API key come from the QR code on your StellAI server. " +
                        "Only connect to your own computer.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Space.Muted,
                )
            }
        },
        confirmButton = { TextButton(onClick = onConfirm) { Text("Connect") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
