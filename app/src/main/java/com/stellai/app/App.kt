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

    var assistantRunning by mutableStateOf(false)
        private set

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
    }

    fun setAppVisible(visible: Boolean) {
        appVisible = visible
        refreshPeriodicSync()
        if (visible) {
            sync.request()
            scope.launch { reminders.refresh() }
        }
    }

    fun onAssistantServiceChanged(running: Boolean) {
        assistantRunning = running
        refreshPeriodicSync()
    }

    /** Applies settings: starts or stops the wake word service. Call from a visible activity. */
    fun applySettings(fromActivity: android.app.Activity) {
        if (settings.wakeWordEnabled && voice.hasMicPermission() && settings.apiKey.isNotBlank()) {
            if (!assistantRunning) AssistantService.start(fromActivity)
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
