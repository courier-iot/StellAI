package com.stellai.app

import android.view.ViewGroup
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.withContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.LaunchedEffect
import android.os.Looper
import android.os.Handler
import android.webkit.WebResourceResponse
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.JavascriptInterface
import androidx.compose.material.icons.outlined.Mic
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
    val main = remember { Handler(Looper.getMainLooper()) }
    var loadError by remember { mutableStateOf<String?>(null) }
    var ready by remember { mutableStateOf(false) }
    var pageError by remember { mutableStateOf("") }
    var timedOut by remember { mutableStateOf(false) }
    var serverOk by remember { mutableStateOf(false) }
    var webView by remember { mutableStateOf<WebView?>(null) }
    // In the background the page sleeps (the phone speaks replies itself); it catches up when you come back
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_STOP -> webView?.onPause()
                Lifecycle.Event.ON_START -> webView?.onResume()
                else -> {}
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    // Check first that the server has an avatar screen (servers from before it was added don't)
    LaunchedEffect(s.serverUrl) {
        if (s.serverUrl.isBlank()) return@LaunchedEffect
        try {
            withContext(Dispatchers.IO) { HubApi.avatarConfig(s.serverUrl, s.apiKey) }
            serverOk = true
        } catch (e: Exception) {
            val m = e.message.orEmpty()
            loadError = when {
                m.startsWith("HTTP 404") -> "Your StellAI server is older than this app and doesn't have the avatar screen yet. " +
                    "Update it: on Windows, StellAI updates itself from GitHub within 30 minutes once a new version is " +
                    "published (or reinstall the latest installer); on Linux, run stellai-setup.sh again."
                m.startsWith("HTTP 401") -> "The server refused the API key. Check it in Settings."
                else -> "Couldn't reach the server: ${app.engine.friendlyError(e)}"
            }
        }
    }
    // If the page hasn't said it's running after a while, show why
    LaunchedEffect(serverOk) {
        if (!serverOk) return@LaunchedEffect
        delay(20_000)
        if (!ready && loadError == null) {
            timedOut = true
            loadError = "The avatar page didn't start" + (if (pageError.isNotBlank()) ": $pageError" else ".") +
                " If this keeps happening, update \"Android System WebView\" and \"Chrome\" in the Play Store."
        }
    }
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
            if (serverOk) AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { ctx ->
                    WebView(ctx).apply {
                        // Without this a WebView inside Compose can end up with no size and show nothing
                        layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                        // Lets you inspect the page from a computer (chrome://inspect) if something goes wrong
                        WebView.setWebContentsDebuggingEnabled(true)
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.mediaPlaybackRequiresUserGesture = false   // so Stella can talk without a tap
                        setBackgroundColor(0xFF04060D.toInt())
                        // The page reports when it's running, and any error that stops it
                        addJavascriptInterface(object {
                            @JavascriptInterface
                            fun ready() = main.post {
                                ready = true
                                // the page was just slow (mobile data): take the "didn't start" message away again
                                if (timedOut) { timedOut = false; loadError = null }
                            }

                            @JavascriptInterface
                            fun error(message: String) = main.post { if (!ready) pageError = message.take(300) }
                        }, "StellaiApp")
                        webChromeClient = object : WebChromeClient() {
                            override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                                if (m.messageLevel() == ConsoleMessage.MessageLevel.ERROR) main.post { if (!ready) pageError = m.message().take(300) }
                                return true
                            }
                        }
                        webViewClient = object : WebViewClient() {
                            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                                if (request.isForMainFrame) loadError = "Couldn't open the avatar screen: ${error.description}. " +
                                    "Is StellAI Server running and up to date?"
                            }

                            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                                if (request.isForMainFrame) loadError = "The server answered the avatar screen with an error " +
                                    "(HTTP ${response.statusCode}). Update StellAI Server, then try again."
                            }
                        }
                        loadUrl(HubApi.avatarPageUrl(s.serverUrl, s.apiKey))
                        webView = this
                    }
                },
                onRelease = { webView = null; it.destroy() },
            )
        }
        loadError?.let { Text(it, color = Space.Red, modifier = Modifier.align(Alignment.Center).padding(24.dp)) }
        if (loadError == null && !ready && s.serverUrl.isNotBlank()) {
            Column(Modifier.align(Alignment.Center), horizontalAlignment = Alignment.CenterHorizontally) {
                CircularProgressIndicator(color = Space.Accent, strokeWidth = 2.dp, modifier = Modifier.size(28.dp))
                Text(if (serverOk) "Waking Stella up…" else "Checking the server…", color = Space.Muted,
                    style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 10.dp))
            }
        }

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
                else Icon(Icons.Outlined.Mic, contentDescription = "Talk", tint = Color(0xFF04060D), modifier = Modifier.size(30.dp))
            }
            voice.lastError?.let { Text(it, color = Space.Red, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 6.dp)) }
        }
    }
}
