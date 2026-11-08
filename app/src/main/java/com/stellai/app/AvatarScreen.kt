package com.stellai.app

import android.Manifest
import android.annotation.SuppressLint
import android.view.WindowManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/**
 * Full-screen avatar (the server's /avatar page): turns a spare phone or tablet into Stella's screen.
 * Talking uses the app's own microphone and wake word; the avatar speaks replies with the server's voice.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun AvatarScreen(onDone: () -> Unit) {
    val app = App.instance
    val s = app.settings
    val voice = app.voice
    val context = LocalContext.current
    var loadError by remember { mutableStateOf<String?>(null) }
    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) voice.listenNow()
    }

    DisposableEffect(Unit) {
        val window = (context as? ComponentActivity)?.window
        window?.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        app.avatarScreenOpen = true
        onDispose {
            app.avatarScreenOpen = false
            window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    Box(Modifier.fillMaxSize().background(Color(0xFF04060D))) {
        if (s.serverUrl.isBlank()) {
            Text("Connect to your StellAI server first (Settings).", Modifier.align(Alignment.Center).padding(24.dp))
        } else {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { ctx ->
                    WebView(ctx).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.mediaPlaybackRequiresUserGesture = false   // so Stella can talk without a tap
                        setBackgroundColor(0xFF04060D.toInt())
                        webViewClient = object : WebViewClient() {
                            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                                if (request.isForMainFrame) loadError = "Couldn't open the avatar screen: ${error.description}. " +
                                    "Is StellAI Server running and up to date?"
                            }
                        }
                        loadUrl(HubApi.avatarPageUrl(s.serverUrl, s.apiKey))
                    }
                },
                onRelease = { it.destroy() },
            )
        }
        loadError?.let { Text(it, color = Space.Red, modifier = Modifier.align(Alignment.Center).padding(24.dp)) }

        IconButton(onClick = onDone, modifier = Modifier.align(Alignment.TopStart).padding(top = 40.dp, start = 4.dp)) {
            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = Space.Muted)
        }

        // Talk button (the wake word works here too when it's turned on)
        Column(Modifier.align(Alignment.BottomCenter).padding(bottom = 28.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            val capturing = voice.phase == VoicePhase.LISTENING || voice.phase == VoicePhase.TRANSCRIBING
            FilledIconButton(
                onClick = {
                    when {
                        capturing -> voice.cancel()
                        voice.hasMicPermission() -> voice.listenNow()
                        else -> micPermission.launch(Manifest.permission.RECORD_AUDIO)
                    }
                },
                modifier = Modifier.size(64.dp),
                shape = CircleShape,
                colors = IconButtonDefaults.filledIconButtonColors(
                    containerColor = if (capturing) Space.Red else Space.Accent.copy(alpha = 0.85f),
                ),
            ) {
                if (capturing) Icon(Icons.Filled.Close, contentDescription = "Stop", tint = Color(0xFF04060D), modifier = Modifier.size(30.dp))
                else Text("🎙", style = MaterialTheme.typography.headlineSmall)
            }
            voice.lastError?.let { Text(it, color = Space.Red, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 6.dp)) }
        }
    }
}
