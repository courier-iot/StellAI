package com.stellai.app

import android.content.Context
import androidx.compose.runtime.mutableStateOf
import kotlin.properties.ReadWriteProperty
import kotlin.reflect.KProperty

/** All app settings. Each property is Compose state and is saved immediately. */
class AppSettings(context: Context) {

    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var serverUrl by stringPref("server_url", "http://192.168.1.100:8080")
    var apiKey by stringPref("api_key", "")
    var thinking by boolPref("thinking", false)
    var assistantName by stringPref("assistant_name", "Stella")
    var accentColor by stringPref("accent_color", "cyan")

    // Phone wake word + voice
    var wakeWordEnabled by boolPref("wake_enabled", false)
    var wakeWord by stringPref("wake_word", "hey_jarvis")
    var wakeSensitivity by intPref("wake_sensitivity", 1)
    var speakReplies by boolPref("speak_replies", true)

    var lastConversation by stringPref("last_conversation", "")

    // Sync bookkeeping (not UI state)
    var syncCursor: Long
        get() = prefs.getLong("sync_cursor", 0L)
        set(v) = prefs.edit().putLong("sync_cursor", v).apply()
    var syncServerId: String
        get() = prefs.getString("sync_server_id", "") ?: ""
        set(v) = prefs.edit().putString("sync_server_id", v).apply()

    val wakeThreshold: Float
        get() = when (wakeSensitivity) {
            0 -> 0.7f
            2 -> 0.3f
            else -> 0.5f
        }

    val wakeWordLabel: String
        get() = WAKE_WORDS.firstOrNull { it.first == wakeWord }?.second ?: "Hey Jarvis"

    // ---- Persistent Compose-state delegates ----------------------------------------
    private fun stringPref(key: String, default: String) = object : ReadWriteProperty<Any?, String> {
        private val state = mutableStateOf(prefs.getString(key, default) ?: default)
        override fun getValue(thisRef: Any?, property: KProperty<*>) = state.value
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: String) {
            state.value = value
            prefs.edit().putString(key, value).apply()
        }
    }

    private fun boolPref(key: String, default: Boolean) = object : ReadWriteProperty<Any?, Boolean> {
        private val state = mutableStateOf(prefs.getBoolean(key, default))
        override fun getValue(thisRef: Any?, property: KProperty<*>) = state.value
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: Boolean) {
            state.value = value
            prefs.edit().putBoolean(key, value).apply()
        }
    }

    private fun intPref(key: String, default: Int) = object : ReadWriteProperty<Any?, Int> {
        private val state = mutableStateOf(prefs.getInt(key, default))
        override fun getValue(thisRef: Any?, property: KProperty<*>) = state.value
        override fun setValue(thisRef: Any?, property: KProperty<*>, value: Int) {
            state.value = value
            prefs.edit().putInt(key, value).apply()
        }
    }

    companion object {
        /** Pre-trained openWakeWord models bundled with the app: id to spoken phrase. */
        val WAKE_WORDS = listOf(
            "hey_jarvis" to "Hey Jarvis",
            "alexa" to "Alexa",
            "hey_mycroft" to "Hey Mycroft",
        )

        fun normalizeUrl(url: String): String {
            val cleaned = url.trim().trimEnd('/')
            return if (cleaned.isNotEmpty() && !cleaned.startsWith("http://") && !cleaned.startsWith("https://")) {
                "http://$cleaned"
            } else cleaned
        }
    }
}
