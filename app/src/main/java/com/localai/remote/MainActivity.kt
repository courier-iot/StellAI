package com.localai.remote

import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext

class MainActivity : ComponentActivity() {

    private val app get() = application as App

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (savedInstanceState == null) handleIntent(intent)
        setContent {
            AppTheme {
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
        const val EXTRA_LISTEN = "com.localai.remote.LISTEN"
    }
}

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = colors, content = content)
}
