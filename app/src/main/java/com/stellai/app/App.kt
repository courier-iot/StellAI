package com.stellai.app

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** App-wide singletons, shared by the UI, the wake word service and the assistant gesture. */
class App : Application() {

    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    lateinit var settings: AppSettings
        private set
    lateinit var store: ChatStore
        private set
    lateinit var sync: SyncManager
        private set
    lateinit var engine: ChatEngine
        private set
    lateinit var voice: VoiceController
        private set
    lateinit var speaker: Speaker
        private set
    lateinit var memory: MemoryManager
        private set
    lateinit var projects: ProjectManager
        private set
    lateinit var modes: ModeManager
        private set
    lateinit var reminders: ReminderManager
        private set

    /** The background service is running (keeps the connection when you switch apps). */
    var assistantRunning by mutableStateOf(false)
        private set

    /** The wake word is being listened for. */
    var wakeListening by mutableStateOf(false)
        private set

    /** True while the avatar screen is open: the avatar speaks replies with the server's voice instead. */
    @Volatile
    var avatarScreenOpen = false

    /** Tells avatar screens what Stella is doing on this phone (listening, thinking, a reply). Best effort. */
    fun avatarEvent(state: String, text: String = "", voice: Boolean = false) {
        if (settings.serverUrl.isBlank()) return
        scope.launch(Dispatchers.IO) {
            try {
                HubApi.avatarEvent(settings.serverUrl, settings.apiKey, org.json.JSONObject()
                    .put("state", state).put("text", text).put("voice", voice)
                    .put("source", "phone-" + android.os.Build.MODEL).put("device", "phone"))
            } catch (_: Exception) {
            }
        }
    }

    private var appVisible = false

    /** True while an app screen is in the foreground. */
    val isVisible: Boolean get() = appVisible
    private var periodicSync: Job? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        settings = AppSettings(this)
        memory = MemoryManager(this)
        projects = ProjectManager(this)
        modes = ModeManager(this)
        reminders = ReminderManager(this)
        ReminderManager.createChannel(this)
        ReminderManager.startBackgroundSync(this)
        store = ChatStore(this)
        sync = SyncManager(this)
        engine = ChatEngine(this)
        voice = VoiceController(this)
        speaker = Speaker(this)
        // Stella moved to another server: follow her there (same API key), and say so once
        HubApi.onMoved = { url, name ->
            scope.launch(Dispatchers.Main) {
                if (settings.serverUrl.trim().trimEnd('/') != url.trimEnd('/')) {
                    settings.serverUrl = url
                    android.widget.Toast.makeText(this@App, "Stella moved to ${name.ifBlank { url }}: connected there",
                        android.widget.Toast.LENGTH_LONG).show()
                    sync.request()
                }
            }
        }
    }

    fun setAppVisible(visible: Boolean) {
        appVisible = visible
        refreshPeriodicSync()
        if (visible) {
            sync.request()
            scope.launch { reminders.refresh() }
        }
    }

    fun onAssistantServiceChanged(running: Boolean, wake: Boolean) {
        assistantRunning = running
        wakeListening = running && wake
        refreshPeriodicSync()
    }

    /** True while the avatar screen is open and on screen (in the background the phone speaks for itself). */
    val avatarSpeaking: Boolean get() = avatarScreenOpen && appVisible

    /**
     * Keeps the phone awake while a reply is on its way, so it still arrives with the screen off or
     * another app open. Released when done (and after 10 minutes at most).
     */
    inline fun <T> awake(block: () -> T): T {
        val lock = (getSystemService(POWER_SERVICE) as android.os.PowerManager)
            .newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "StellAI:reply")
        lock.setReferenceCounted(false)
        lock.acquire(10 * 60_000L)
        try {
            return block()
        } finally {
            if (lock.isHeld) lock.release()
        }
    }

    /** Applies settings: starts or stops the wake word service. Call from a visible activity. */
    fun applySettings(fromActivity: android.app.Activity) {
        val wake = settings.wakeWordEnabled && voice.hasMicPermission()
        if ((wake || settings.stayConnected) && settings.apiKey.isNotBlank() && settings.serverUrl.isNotBlank()) {
            // (also when already running: it re-checks whether the wake word is wanted)
            if (!assistantRunning || wake != wakeListening) AssistantService.start(fromActivity)
        } else if (assistantRunning) {
            AssistantService.stop(fromActivity)
        }
        refreshPeriodicSync()
        sync.request()
    }

    /** Sync every minute while the app is open or the assistant is listening. */
    private fun refreshPeriodicSync() {
        val active = appVisible || assistantRunning
        if (active && periodicSync?.isActive != true) {
            periodicSync = scope.launch {
                while (isActive) {
                    delay(60_000)
                    sync.syncNow()
                }
            }
        } else if (!active) {
            periodicSync?.cancel()
            periodicSync = null
        }
    }

    companion object {
        lateinit var instance: App
            private set
    }
}
