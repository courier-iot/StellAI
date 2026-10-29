package com.stellai.app

import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException

enum class Role { USER, ASSISTANT, TOOL, ERROR }

data class UiMessage(
    val id: String,
    val role: Role,
    val text: String,
    val reasoning: String = "",
)

/**
 * The conversation logic, shared by the chat screen, the wake-word service and
 * the assistant gesture. Lives as long as the app process. All state is main-thread.
 */
class ChatEngine(private val app: App) {

    private val client = LlamaClient()
    private val main = Handler(Looper.getMainLooper())

    val conversations = mutableStateListOf<Conversation>()
    var currentId by mutableStateOf<String?>(null)
        private set
    val messages = mutableStateListOf<UiMessage>()
    var busy by mutableStateOf(false)
        private set
    var status by mutableStateOf("")
        private set

    /** Text in the message box. */
    var draft by mutableStateOf("")

    /** Title of the open chat, or "" for a new one. */
    val currentTitle: String
        get() = conversations.firstOrNull { it.id == currentId }?.title ?: ""

    private var job: Job? = null
    @Volatile
    private var stopRequested = false
    /** Bumped when switching chats so a cancelled reply can't write into the new one. */
    private var generation = 0
    private var lastTs = 0L

    init {
        reloadConversations()
        val last = app.settings.lastConversation
        if (last.isNotEmpty() && conversations.any { it.id == last }) {
            currentId = last
            loadMessages(last)
        }
    }

    // ---- Conversations -----------------------------------------------------------------
    fun reloadConversations() {
        val list = app.store.conversations()
        conversations.clear()
        conversations.addAll(list)
    }

    private fun loadMessages(id: String) {
        val stored = app.store.messages(id)
        messages.clear()
        messages.addAll(stored.map { it.toUi() })
    }

    /** Called after sync brought in changes from another device. */
    fun onRemoteChanges() {
        reloadConversations()
        val id = currentId ?: return
        if (busy) return
        if (conversations.none { it.id == id }) {
            newChat() // deleted on another device
        } else {
            loadMessages(id)
        }
    }

    fun open(id: String) {
        abandonTurn()
        currentId = id
        app.settings.lastConversation = id
        loadMessages(id)
    }

    fun newChat() {
        abandonTurn()
        currentId = null
        app.settings.lastConversation = ""
        messages.clear()
        draft = ""
    }

    fun delete(id: String) {
        if (id == currentId) newChat()
        app.store.deleteConversation(id, nextTs())
        reloadConversations()
        app.sync.request()
    }

    private fun abandonTurn() {
        if (busy) stop()
        generation++
        busy = false
        status = ""
    }

    // ---- Sending -----------------------------------------------------------------------------
    fun send(text: String, fromVoice: Boolean = false) {
        val trimmed = text.trim()
        if (trimmed.isEmpty() || busy) return
        val convId = currentId ?: UUID.randomUUID().toString().also { id ->
            val now = nextTs()
            app.store.saveConversation(Conversation(id, titleFrom(trimmed), now, now))
            currentId = id
            app.settings.lastConversation = id
            reloadConversations()
        }
        addStored(convId, Role.USER, trimmed)
        stopRequested = false
        busy = true
        job = app.scope.launch { runConversation(convId, fromVoice) }
    }

    fun stop() {
        stopRequested = true
        client.cancel()
        job?.cancel()
    }

    private suspend fun runConversation(convId: String, fromVoice: Boolean) {
        val gen = generation
        val base = buildHistory(messages.toList())
        val turn = mutableListOf<JSONObject>() // this turn's tool calls and results, in API format
        var finalReply = ""
        try {
            repeat(MAX_TOOL_ROUNDS) {
                status = "Contacting server…"
                val streamId = STREAMING_PREFIX + UUID.randomUUID()
                messages.add(UiMessage(streamId, Role.ASSISTANT, ""))

                val result = withContext(Dispatchers.IO) {
                    client.streamChat(app.settings.serverUrl, app.settings.apiKey, buildRequest(base, turn)) { content, reasoning ->
                        main.post {
                            if (!busy || gen != generation) return@post
                            val visible = stripThink(content)
                            status = if (visible.isBlank() && reasoning.isNotBlank()) "Thinking…" else "Writing…"
                            updateMessage(streamId) { it.copy(text = visible, reasoning = reasoning) }
                        }
                    }
                }
                messages.removeAll { it.id == streamId }

                val finalText = stripThink(result.content).trim()
                if (finalText.isNotEmpty() || result.toolCalls.isEmpty()) {
                    val shown = finalText.ifEmpty { "(no reply)" }
                    addStored(convId, Role.ASSISTANT, shown, result.reasoning)
                    if (finalText.isNotEmpty()) finalReply = finalText
                }

                val assistant = JSONObject().put("role", "assistant").put("content", finalText)
                if (result.toolCalls.isNotEmpty()) {
                    val calls = JSONArray()
                    result.toolCalls.forEach { tc ->
                        calls.put(JSONObject()
                            .put("id", tc.id)
                            .put("type", "function")
                            .put("function", JSONObject().put("name", tc.name).put("arguments", tc.arguments)))
                    }
                    assistant.put("tool_calls", calls)
                }
                turn.add(assistant)

                if (result.toolCalls.isEmpty()) return

                for (tc in result.toolCalls) {
                    status = PhoneTools.displayName(tc.name) + "…"
                    val outcome = PhoneTools.execute(app, tc.name, tc.arguments)
                    addStored(convId, Role.TOOL, outcome.summary)
                    turn.add(JSONObject()
                        .put("role", "tool")
                        .put("tool_call_id", tc.id)
                        .put("name", tc.name)
                        .put("content", outcome.result))
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (!stopRequested && gen == generation) {
                messages.add(UiMessage(UUID.randomUUID().toString(), Role.ERROR, friendlyError(e)))
            }
            if (fromVoice && !stopRequested && app.settings.speakReplies) {
                app.speaker.speak("Sorry, I couldn't get an answer from the server.")
            }
        } finally {
            if (gen == generation) {
                messages.removeAll { it.id.startsWith(STREAMING_PREFIX) }
                if (stopRequested) messages.add(UiMessage(UUID.randomUUID().toString(), Role.ERROR, "Stopped."))
                busy = false
                status = ""
            }
            app.store.touchConversation(convId, nextTs())
            reloadConversations()
            app.sync.request()
            if (fromVoice && finalReply.isNotEmpty() && !stopRequested && app.settings.speakReplies) {
                app.speaker.speak(finalReply)
            }
        }
    }

    private fun addStored(convId: String, role: Role, text: String, reasoning: String = "") {
        val ts = nextTs()
        val m = StoredMessage(UUID.randomUUID().toString(), convId, role, text, reasoning, ts, ts)
        app.store.saveMessage(m)
        if (convId == currentId) messages.add(m.toUi())
    }

    private fun updateMessage(id: String, change: (UiMessage) -> UiMessage) {
        val i = messages.indexOfFirst { it.id == id }
        if (i >= 0) messages[i] = change(messages[i])
    }

    /** Strictly increasing timestamps, so messages keep their order across devices. */
    private fun nextTs(): Long {
        val now = System.currentTimeMillis()
        lastTs = if (now > lastTs) now else lastTs + 1
        return lastTs
    }

    // ---- Prompt building ---------------------------------------------------------------------
    /**
     * Turns the stored chat into API messages. Past phone actions are folded into the
     * assistant's turn as notes, so a chat continued on another device keeps its context.
     */
    private fun buildHistory(list: List<UiMessage>): List<JSONObject> {
        val out = mutableListOf<JSONObject>()
        val pending = StringBuilder()
        fun flush() {
            if (pending.isNotBlank()) {
                out.add(JSONObject().put("role", "assistant").put("content", pending.toString().trim()))
            }
            pending.clear()
        }
        for (m in list) {
            when (m.role) {
                Role.USER -> {
                    flush()
                    out.add(JSONObject().put("role", "user").put("content", m.text))
                }
                Role.ASSISTANT -> if (m.text.isNotBlank() && !m.id.startsWith(STREAMING_PREFIX)) {
                    pending.append(m.text).append('\n')
                }
                Role.TOOL -> pending.append("(done on phone: ").append(m.text).append(")\n")
                Role.ERROR -> {}
            }
        }
        flush()
        if (out.size <= MAX_HISTORY) return out
        var start = out.size - MAX_HISTORY
        while (start < out.size && out[start].optString("role") != "user") start++
        return out.subList(start, out.size)
    }

    private fun buildRequest(base: List<JSONObject>, turn: List<JSONObject>): JSONObject {
        val all = JSONArray()
        all.put(JSONObject().put("role", "system").put("content", systemPrompt()))
        base.forEach { all.put(it) }
        turn.forEach { all.put(it) }
        return JSONObject()
            .put("model", "local")
            .put("messages", all)
            .put("tools", PhoneTools.definitions())
            .put("chat_template_kwargs", JSONObject().put("enable_thinking", app.settings.thinking))
    }

    private fun systemPrompt(): String {
        val now = ZonedDateTime.now()
            .format(DateTimeFormatter.ofPattern("EEEE, yyyy-MM-dd HH:mm (zzzz)", Locale.ENGLISH))
        val name = app.settings.assistantName.ifBlank { "Stella" }
        return """
            You are $name, the user's personal AI assistant in StellAI. You run privately on the user's own computer, not in the cloud.
            You have a warm, capable personality with a light touch of space-explorer flair, but you stay concise and practical.
            The user talks to you through an app on their Android phone, and you can control the phone with the provided tools.
            When the user asks you to do something on the phone, call the matching tool instead of explaining how to do it.
            After a tool runs, briefly confirm what happened. Never claim an action succeeded if the tool reported an error.
            Keep replies short and conversational, in plain text without markdown, because replies may be read aloud.
            The current date and time on the phone is $now.
        """.trimIndent()
    }

    private fun stripThink(text: String): String =
        text.replace(Regex("<think>[\\s\\S]*?(</think>|$)"), "").trimStart()

    private fun titleFrom(text: String): String {
        val oneLine = text.replace(Regex("\\s+"), " ").trim()
        return if (oneLine.length <= 48) oneLine else oneLine.take(47).trimEnd() + "…"
    }

    fun friendlyError(e: Exception): String {
        val msg = e.message.orEmpty()
        return when {
            e is java.net.ConnectException || msg.contains("Failed to connect") ->
                "Can't reach the server at ${app.settings.serverUrl}. Is the server running, and is the phone on the same Wi-Fi or Tailscale?"
            e is java.net.SocketTimeoutException -> "The server took too long to respond."
            e is java.net.UnknownHostException -> "That server address doesn't look right. Check Settings."
            msg.contains("HTTP 401") -> "The API key was rejected. Copy it again from the server's terminal into Settings."
            msg.contains("HTTP 404") -> "The server is out of date. Re-run the setup script on it."
            else -> msg.ifBlank { e.javaClass.simpleName }
        }
    }

    companion object {
        private const val MAX_TOOL_ROUNDS = 5
        private const val MAX_HISTORY = 24
        private const val STREAMING_PREFIX = "streaming-"
    }
}
