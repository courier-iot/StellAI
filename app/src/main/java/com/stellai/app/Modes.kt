package com.stellai.app

import androidx.compose.runtime.mutableStateListOf
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * A mindset for a chat: extra instructions plus how creative Stella is, whether she thinks first,
 * which tools she gets, and how the chat is summarized when its context is refreshed.
 */
data class Mode(
    val id: String,
    val name: String,
    val icon: String,
    val description: String,
    val prompt: String,
    /** null = the model's default. */
    val temperature: Double? = null,
    /** "on", "off" or "default" (the Settings switch). */
    val thinking: String = "default",
    /** "all", or "minimal": no calculators or diagrams (just phone actions, memory and project tools). */
    val tools: String = "all",
    /** "work" or "story": what a context refresh keeps. */
    val style: String = "work",
    val builtIn: Boolean = true,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("name", name).put("icon", icon).put("prompt", prompt)
        .put("temperature", temperature ?: JSONObject.NULL)
        .put("thinking", thinking).put("tools", tools).put("style", style)

    companion object {
        fun fromJson(o: JSONObject, builtIn: Boolean = false) = Mode(
            id = o.optString("id"),
            name = o.optString("name"),
            icon = o.optString("icon").ifBlank { "✦" },
            description = if (builtIn) o.optString("desc") else "Your mode",
            prompt = o.optString("prompt"),
            temperature = if (o.isNull("temperature") || !o.has("temperature")) null else o.optDouble("temperature"),
            thinking = o.optString("thinking", "default"),
            tools = o.optString("tools", "all"),
            style = o.optString("style", "work"),
            builtIn = builtIn,
        )
    }
}

val GENERAL_MODE = Mode("general", "General", "✦", "Everyday help, phone actions and quick answers", "")

/**
 * Stella's instructions and the built-in modes come from the server (GET /prompts, one copy shared with the desktop
 * chat) and are cached, so the phone has them when the server is briefly unreachable. Until the first answer there's
 * only the General mode and short fallback instructions.
 */
class Prompts(
    val instructions: String,
    val modeNote: String,
    val summaryIntro: String,
    val voiceStyle: String,
) {
    companion object {
        val FALLBACK = Prompts(
            "You are {name}, the user's private AI assistant (StellAI), running on their own computer. Concise and " +
                "practical. The user is on their Android phone; to do things on it, call the tools.",
            "",
            "The start of this conversation was summarized to free up memory; continue naturally from it.",
            "Voice conversation: answer in one to three short spoken sentences, without markdown.",
        )

        fun from(o: JSONObject): Prompts? {
            val text = o.optJSONObject("instructions")?.optString("phone").orEmpty()
            if (text.isBlank()) return null
            return Prompts(text, o.optString("mode_note"), o.optString("summary_intro"), o.optString("voice_style"))
        }
    }
}

/** Built-in modes (from the server) plus the user's own (kept on the server so every device has them). */
class ModeManager(private val app: App) {

    val builtIn = mutableStateListOf(GENERAL_MODE)
    val custom = mutableStateListOf<Mode>()
    var prompts: Prompts = Prompts.FALLBACK
        private set

    init {
        try {
            custom.addAll(parse(JSONArray(app.settings.modeCache)))
        } catch (_: Exception) {
        }
        try {
            applyPrompts(JSONObject(app.settings.promptCache))
        } catch (_: Exception) {
        }
    }

    val all: List<Mode> get() = builtIn + custom

    fun get(id: String?): Mode =
        if (id.isNullOrEmpty()) builtIn.first() else all.firstOrNull { it.id == id } ?: builtIn.first()

    private fun parse(arr: JSONArray, builtIn: Boolean = false) = (0 until arr.length()).mapNotNull { i ->
        arr.optJSONObject(i)?.let { Mode.fromJson(it, builtIn) }
    }.filter { it.id.isNotBlank() }

    private fun applyPrompts(o: JSONObject): Boolean {
        val p = Prompts.from(o) ?: return false
        val modes = parse(o.optJSONArray("modes") ?: JSONArray(), builtIn = true)
        if (modes.none { it.id == GENERAL_MODE.id }) return false
        prompts = p
        builtIn.clear()
        builtIn.addAll(modes)
        return true
    }

    suspend fun refresh(): String? = try {
        try {   // an older server has no /prompts yet: keep the cached ones and still load your modes
            val o = withContext(Dispatchers.IO) { HubApi.prompts(app.settings.serverUrl, app.settings.apiKey) }
            if (applyPrompts(o)) app.settings.promptCache = o.toString()
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
        }
        val arr = withContext(Dispatchers.IO) { HubApi.modes(app.settings.serverUrl, app.settings.apiKey) }
        custom.clear()
        custom.addAll(parse(arr))
        app.settings.modeCache = arr.toString()
        null
    } catch (e: Exception) {
        app.engine.friendlyError(e)
    }

    /** Saves a custom mode; returns an error message or null. */
    suspend fun save(mode: Mode): String? = try {
        val body = mode.toJson()
        if (mode.id.isBlank()) body.remove("id")
        withContext(Dispatchers.IO) { HubApi.saveMode(app.settings.serverUrl, app.settings.apiKey, body) }
        refresh()
    } catch (e: Exception) {
        app.engine.friendlyError(e).removePrefix("HTTP 400: ")
    }

    suspend fun delete(id: String): String? = try {
        withContext(Dispatchers.IO) { HubApi.deleteMode(app.settings.serverUrl, app.settings.apiKey, id) }
        refresh()
    } catch (e: Exception) {
        app.engine.friendlyError(e)
    }
}
