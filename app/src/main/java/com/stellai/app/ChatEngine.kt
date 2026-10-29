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

    /** Photos waiting to be sent with the next message (ids in MediaCache). */
    val attachments = mutableStateListOf<String>()

    /** Set while editing an earlier message: sending replaces it and everything after it. */
    var editingId by mutableStateOf<String?>(null)
        private set

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
        attachments.clear()
        editingId = null
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
        editingId = null
    }

    // ---- Photos ----------------------------------------------------------------------------
    /** Adds a (already resized) JPEG to the next message. */
    fun addPhoto(jpeg: ByteArray) {
        val id = UUID.randomUUID().toString().replace("-", "").take(16)
        MediaCache.saveLocal(app, id, jpeg)
        attachments.add(id)
    }

    fun removePhoto(id: String) {
        attachments.remove(id)
    }

    // ---- Message actions -----------------------------------------------------------------
    /** Removes a message and everything after it (on all devices). */
    private fun truncateFrom(messageId: String) {
        val idx = messages.indexOfFirst { it.id == messageId }
        if (idx < 0) return
        val gone = messages.subList(idx, messages.size).toList()
        app.store.deleteMessages(gone.filter { it.role != Role.ERROR }.map { it.id }, nextTs())
        messages.removeAll(gone)
    }

    /** Asks Stella to answer the last message again. */
    fun retry() {
        if (busy) return
        val convId = currentId ?: return
        val lastUser = messages.indexOfLast { it.role == Role.USER }
        if (lastUser < 0) return
        if (lastUser + 1 < messages.size) truncateFrom(messages[lastUser + 1].id)
        stopRequested = false
        busy = true
        job = app.scope.launch { runConversation(convId, fromVoice = false, newPhotos = emptyList()) }
    }

    /** Puts an earlier message back in the box; sending replaces it and the replies after it. */
    fun startEdit(message: UiMessage) {
        if (busy || message.role != Role.USER) return
        editingId = message.id
        attachments.clear()
        attachments.addAll(PHOTO_REF.findAll(message.text).map { it.groupValues[1] })
        draft = message.text.replace(PHOTO_REF, "").trim()
    }

    fun cancelEdit() {
        editingId = null
        draft = ""
        attachments.clear()
    }

    // ---- Sending -----------------------------------------------------------------------------
    fun send(text: String, fromVoice: Boolean = false) {
        val trimmed = text.trim()
        val photos = attachments.toList()
        if ((trimmed.isEmpty() && photos.isEmpty()) || busy) return
        editingId?.let { truncateFrom(it) }
        editingId = null
        val content = buildString {
            append(trimmed)
            photos.forEach { append("\n![photo](image:").append(it).append(")") }
        }.trim()
        attachments.clear()
        val convId = currentId ?: UUID.randomUUID().toString().also { id ->
            val now = nextTs()
            app.store.saveConversation(Conversation(id, titleFrom(trimmed.ifEmpty { "Photo" }), now, now))
            currentId = id
            app.settings.lastConversation = id
            reloadConversations()
        }
        addStored(convId, Role.USER, content)
        stopRequested = false
        busy = true
        job = app.scope.launch { runConversation(convId, fromVoice, photos) }
    }

    fun stop() {
        stopRequested = true
        client.cancel()
        job?.cancel()
    }

    private suspend fun runConversation(convId: String, fromVoice: Boolean, newPhotos: List<String>) {
        val gen = generation
        val snapshot = messages.toList()
        var memoryChanged = false
        status = "Contacting server…"
        val base = withContext(Dispatchers.IO) {
            // Share new photos with your other devices (the model gets them directly below).
            for (id in newPhotos) {
                try {
                    HubApi.uploadImage(app.settings.serverUrl, app.settings.apiKey, id, MediaCache.bytes(app, "image:$id"))
                } catch (_: Exception) {
                }
            }
            ServerTools.refresh(app)
            buildHistory(snapshot)
        }
        app.memory.refresh()
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
                    val onServer = ServerTools.has(tc.name)
                    if (tc.name == "remember" || tc.name == "forget") memoryChanged = true
                    status = (if (onServer) ServerTools.displayName(tc.name) else PhoneTools.displayName(tc.name)) + "…"
                    val outcome = if (onServer) {
                        withContext(Dispatchers.IO) { ServerTools.execute(app, tc.name, tc.arguments) }
                    } else {
                        PhoneTools.execute(app, tc.name, tc.arguments)
                    }
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
            if (memoryChanged) app.scope.launch { app.memory.refresh() }
            app.memory.scheduleLearning(convId)
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
        val lastUserId = list.lastOrNull { it.role == Role.USER }?.id
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
                    out.add(JSONObject().put("role", "user").put("content", userContent(m, m.id == lastUserId)))
                }
                Role.ASSISTANT -> if (m.text.isNotBlank() && !m.id.startsWith(STREAMING_PREFIX)) {
                    pending.append(m.text).append('\n')
                }
                Role.TOOL -> pending.append("(action result: ")
                    .append(m.text.replace(DIAGRAM_REF, "[diagram shown]").trim())
                    .append(")\n")
                Role.ERROR -> {}
            }
        }
        flush()
        if (out.size <= MAX_HISTORY) return out
        var start = out.size - MAX_HISTORY
        while (start < out.size && out[start].optString("role") != "user") start++
        return out.subList(start, out.size)
    }

    /** Text, or text + images (OpenAI format) for the newest message with photos. */
    private fun userContent(m: UiMessage, withImages: Boolean): Any {
        val ids = PHOTO_REF.findAll(m.text).map { it.groupValues[1] }.toList()
        val text = m.text.replace(PHOTO_REF, "").trim()
        if (ids.isEmpty()) return text
        if (!withImages) return (text + " " + ids.joinToString(" ") { "[photo]" }).trim()
        val parts = JSONArray()
        parts.put(JSONObject().put("type", "text").put("text", text.ifEmpty { "(The user sent a photo.)" }))
        for (id in ids) {
            val bytes = try {
                MediaCache.bytes(app, "image:$id")
            } catch (_: Exception) {
                null
            } ?: continue
            val b64 = android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)
            parts.put(JSONObject().put("type", "image_url")
                .put("image_url", JSONObject().put("url", "data:image/jpeg;base64,$b64")))
        }
        return parts
    }

    private fun buildRequest(base: List<JSONObject>, turn: List<JSONObject>): JSONObject {
        val all = JSONArray()
        all.put(JSONObject().put("role", "system").put("content", systemPrompt()))
        base.forEach { all.put(it) }
        turn.forEach { all.put(it) }
        return JSONObject()
            .put("model", "local")
            .put("messages", all)
            .put("tools", JSONArray().apply {
                val phone = PhoneTools.definitions()
                for (i in 0 until phone.length()) put(phone.get(i))
                val server = ServerTools.definitions()
                for (i in 0 until server.length()) put(server.get(i))
            })
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
            Keep replies concise and conversational. You may use Markdown (bold, lists, short headings). Always put code in fenced code blocks with a language tag. Avoid tables.
            For ANY arithmetic or electronics maths (resistors, dividers, LEDs, filters, power, unit conversions), call the calculate or electronics tools instead of working it out yourself, then explain the result.
            When a picture would help (circuit schematic, block diagram, flowchart, state machine, timing diagram), call render_diagram. It appears in the chat automatically, so don't repeat its code unless asked. If it fails, fix the code and try again once.
            The user may send photos (circuits, screens, sketches, parts). Describe what you actually see and say so if something is unclear.
            When the user shares a lasting fact about themselves or their projects, or asks you to remember something, call remember. If they ask you to forget something, call forget.
            The current date and time on the phone is $now.
        """.trimIndent() + app.memory.promptSection().let { if (it.isEmpty()) "" else "\n\n$it" }
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
            msg.contains("image input is not supported", ignoreCase = true) || msg.contains("mmproj", ignoreCase = true) ->
                "Photo understanding isn't set up on the server. Re-run the setup script and say yes to the vision add-on."
            else -> msg.ifBlank { e.javaClass.simpleName }
        }
    }

    companion object {
        val PHOTO_REF = Regex("!\\[[^\\]]*\\]\\(image:([0-9a-f]{16})\\)")
        private val DIAGRAM_REF = Regex("!\\[[^\\]]*\\]\\(diagram:[0-9a-f]+\\)")
        private const val MAX_TOOL_ROUNDS = 6
        private const val MAX_HISTORY = 24
        private const val STREAMING_PREFIX = "streaming-"
    }
}
