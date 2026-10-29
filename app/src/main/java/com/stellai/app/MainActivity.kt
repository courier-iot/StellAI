package com.stellai.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue

class MainActivity : ComponentActivity() {

    private val app get() = application as App

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
                var showSettings by rememberSaveable { mutableStateOf(app.settings.apiKey.isBlank()) }
                if (showSettings) {
                    BackHandler { showSettings = false }
                    SettingsScreen(onDone = { showSettings = false })
                } else {
                    ChatScreen(onOpenSettings = { showSettings = true })
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
        val listen = intent.getBooleanExtra(EXTRA_LISTEN, false) ||
            intent.action == Intent.ACTION_ASSIST ||
            intent.action == Intent.ACTION_VOICE_COMMAND
        if (!listen) return
        intent.removeExtra(EXTRA_LISTEN)
        if (app.voice.hasMicPermission() && app.settings.apiKey.isNotBlank()) app.voice.listenNow()
    }

    companion object {
        const val EXTRA_LISTEN = "com.stellai.app.LISTEN"
    }
}
