package com.stellai.app

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

data class Memory(val id: String, val text: String, val source: String)

/**
 * Stella's long-term memory. The facts live on the server (shared by all your devices);
 * this keeps a copy for the prompt and the Memory screen, and asks the server to learn
 * new facts from a chat once it has gone quiet.
 */
class MemoryManager(private val app: App) {

    val items = mutableStateListOf<Memory>()
    var status by mutableStateOf("")
        private set
    /** Short note shown in the chat after Stella learns something, e.g. "Learned 2 new things". */
    var lastLearned by mutableStateOf<String?>(null)

    private var learnJob: Job? = null
    private val learnedUpTo = mutableMapOf<String, String>() // conversation id -> last message id learned from

    init {
        items.addAll(parse(JSONArray(app.settings.memoryCache)))
    }

    private fun parse(arr: JSONArray): List<Memory> = (0 until arr.length()).mapNotNull { i ->
        arr.optJSONObject(i)?.let { Memory(it.optString("id"), it.optString("text"), it.optString("source")) }
    }.filter { it.id.isNotBlank() && it.text.isNotBlank() }

    /** Fetches the latest list from the server. Returns false if it couldn't reach it. */
    suspend fun refresh(): Boolean = try {
        val arr = withContext(Dispatchers.IO) { HubApi.memories(app.settings.serverUrl, app.settings.apiKey) }
        items.clear()
        items.addAll(parse(arr))
        app.settings.memoryCache = arr.toString()
        status = ""
        true
    } catch (e: Exception) {
        status = "Couldn't reach the server: ${app.engine.friendlyError(e)}"
        false
    }

    suspend fun add(text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        try {
            withContext(Dispatchers.IO) { HubApi.addMemory(app.settings.serverUrl, app.settings.apiKey, t) }
            refresh()
        } catch (e: Exception) {
            status = "Couldn't save: ${app.engine.friendlyError(e)}"
        }
    }

    suspend fun delete(id: String) {
        try {
            withContext(Dispatchers.IO) { HubApi.deleteMemory(app.settings.serverUrl, app.settings.apiKey, id) }
            items.removeAll { it.id == id }
            refresh()
        } catch (e: Exception) {
            status = "Couldn't delete: ${app.engine.friendlyError(e)}"
        }
    }

    /** The part of Stella's instructions listing what she knows about you. */
    fun promptSection(): String {
        if (items.isEmpty()) return ""
        val sb = StringBuilder("What you remember about the user from earlier conversations (id in brackets, for the forget tool):\n")
        var chars = 0
        for (m in items) {
            val line = "- [${m.id}] ${m.text}\n"
            if (chars + line.length > 3500) break
            sb.append(line)
            chars += line.length
        }
        sb.append("Use these naturally when relevant; don't recite them.")
        return sb.toString()
    }

    /**
     * After a chat goes quiet for a while, ask the server to pick out anything worth
     * remembering. Runs on the server's model, so it waits until you're not mid-conversation.
     */
    fun scheduleLearning(convId: String) {
        if (!app.settings.learnFromChats || app.settings.apiKey.isBlank()) return
        learnJob?.cancel()
        learnJob = app.scope.launch {
            delay(45_000)
            if (app.engine.busy) return@launch
            val msgs = withContext(Dispatchers.IO) { app.store.messages(convId) }
                .filter { it.role == Role.USER || it.role == Role.ASSISTANT }
                .takeLast(12)
            val last = msgs.lastOrNull() ?: return@launch
            if (learnedUpTo[convId] == last.id || msgs.none { it.role == Role.USER }) return@launch
            val arr = JSONArray()
            msgs.forEach { m ->
                arr.put(JSONObject()
                    .put("role", if (m.role == Role.USER) "user" else "assistant")
                    .put("text", m.text.replace(IMAGE_REF, "[photo]")))
            }
            try {
                val result = withContext(Dispatchers.IO) { HubApi.learn(app.settings.serverUrl, app.settings.apiKey, arr) }
                learnedUpTo[convId] = last.id
                val added = result.optJSONArray("added")?.length() ?: 0
                val removed = result.optJSONArray("removed")?.length() ?: 0
                if (added + removed > 0) {
                    refresh()
                    lastLearned = when {
                        added > 0 && removed > 0 -> "Updated my memory ($added new, $removed replaced)"
                        added == 1 -> "Learned 1 new thing about you"
                        added > 1 -> "Learned $added new things about you"
                        else -> "Forgot $removed outdated thing${if (removed > 1) "s" else ""}"
                    }
                }
            } catch (_: Exception) {
                // Server busy or offline: try again after the next message.
            }
        }
    }

    companion object {
        val IMAGE_REF = Regex("!\\[[^\\]]*\\]\\((?:image|diagram):[0-9a-f]+\\)")
    }
}
