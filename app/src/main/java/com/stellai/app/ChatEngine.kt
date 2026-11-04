package com.stellai.app

import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CompletableDeferred
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

enum class Role { USER, ASSISTANT, TOOL, ERROR, SUMMARY }

/** A server action waiting for the user's OK (e.g. a GitHub commit). */
class Approval(val tool: String, val args: JSONObject) {
    internal val answer = CompletableDeferred<Boolean>()
    fun approve() = answer.complete(true)
    fun decline() = answer.complete(false)
}

/** How full Stella's context window is for the open chat, in tokens. */
data class ContextInfo(val used: Int, val limit: Int, val nCtx: Int, val projectTokens: Int) {
    val fraction: Float get() = if (limit <= 0) 0f else (used.toFloat() / limit).coerceIn(0f, 1f)
}

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

    /** Project for the next new chat (when no chat is open yet). */
    var pendingProjectId by mutableStateOf("")
        private set

    /** Project of the open chat, or of the chat about to be started. "" = none. */
    val currentProjectId: String
        get() = currentId?.let { id -> conversations.firstOrNull { it.id == id }?.projectId } ?: pendingProjectId

    /** Mode for the next new chat (when no chat is open yet). "" = General. */
    var pendingMode by mutableStateOf("")
        private set

    /** Mindset of the open chat (or of the chat about to be started). */
    val currentMode: Mode
        get() = app.modes.get(currentId?.let { id -> conversations.firstOrNull { it.id == id }?.mode } ?: pendingMode)

    private fun modeOf(convId: String): Mode = app.modes.get(conversations.firstOrNull { it.id == convId }?.mode)

    /** Context window usage for the open chat (null until the server has measured it). */
    var context by mutableStateOf<ContextInfo?>(null)
        private set

    /** Last problem refreshing the context, shown in the chat. */
    var contextNotice by mutableStateOf<String?>(null)

    private var meterJob: Job? = null

    /** Tool names already offered in each chat; kept so the tool list stays stable (see select_tools on the server). */
    private val stickyTools = mutableMapOf<String, MutableSet<String>>()

    /** Shown as a dialog in the chat until the user approves or declines. */
    var pendingApproval by mutableStateOf<Approval?>(null)
        private set

    private suspend fun askApproval(tool: String, args: JSONObject): Boolean {
        if (!app.isVisible) return false   // e.g. a voice command with the screen off: never commit unseen
        val a = Approval(tool, args)
        pendingApproval = a
        return try {
            a.answer.await()
        } finally {
            if (pendingApproval === a) pendingApproval = null
        }
    }

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
        pendingProjectId = ""
        pendingMode = ""
        app.settings.lastConversation = id
        loadMessages(id)
        context = null
        updateMeter()
    }

    /** Starts a new chat, optionally inside a project. */
    fun newChat(projectId: String = "") {
        abandonTurn()
        currentId = null
        pendingProjectId = projectId
        pendingMode = ""
        app.settings.lastConversation = ""
        messages.clear()
        draft = ""
        attachments.clear()
        editingId = null
        context = null
        contextNotice = null
    }

    /** Switches the open chat's mindset (or the next new chat's). Takes effect from the next reply. */
    fun setMode(modeId: String) {
        val id = currentId
        val value = if (modeId == GENERAL_MODE.id) "" else modeId
        if (id == null) {
            pendingMode = value
            return
        }
        app.store.setMode(id, value, nextTs())
        reloadConversations()
        app.sync.request()
        updateMeter()
    }

    /** Moves a chat into a project, or out of one with "". */
    fun moveToProject(convId: String?, projectId: String) {
        if (convId == null) {
            pendingProjectId = projectId
            return
        }
        app.store.setProject(convId, projectId, nextTs())
        reloadConversations()
        app.sync.request()
        if (convId == currentId) updateMeter()
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
            app.store.saveConversation(Conversation(id, titleFrom(trimmed.ifEmpty { "Photo" }), now, now,
                projectId = pendingProjectId, mode = pendingMode))
            pendingProjectId = ""
            pendingMode = ""
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
        var memoryChanged = false
        var projectChanged = false
        var remindersChanged = false
        status = "Contacting server…"
        val projectId = conversations.firstOrNull { it.id == convId }?.projectId.orEmpty()
        val mode = modeOf(convId)
        val turn = mutableListOf<JSONObject>() // this turn's tool calls and results, in API format
        var finalReply = ""
        try {
            withContext(Dispatchers.IO) {
                // Share new photos with your other devices (the model gets them directly below).
                for (id in newPhotos) {
                    try {
                        HubApi.uploadImage(app.settings.serverUrl, app.settings.apiKey, id, MediaCache.bytes(app, "image:$id"))
                    } catch (_: Exception) {
                    }
                }
                ServerTools.refresh(app)
            }
            app.memory.refresh()
            // Fit the chat into Stella's context window; when it's nearly full, refresh it with a summary.
            var prepared = prepare(convId, projectId, newPhotos.size, mode, fromVoice)
            if (prepared.needsRefresh && !stopRequested && gen == generation) {
                status = "Refreshing context…"
                if (compact(convId, keepUserTurns = 2)) prepared = prepare(convId, projectId, newPhotos.size, mode, fromVoice)
            }
            if (gen == generation && prepared.info != null) context = prepared.info
            rememberTools(convId, prepared.tools)
            val base = prepared.history
            val system = prepared.system
            val tools = prepared.tools
            repeat(MAX_TOOL_ROUNDS) {
                status = "Contacting server…"
                val streamId = STREAMING_PREFIX + UUID.randomUUID()
                messages.add(UiMessage(streamId, Role.ASSISTANT, ""))

                val result = withContext(Dispatchers.IO) {
                    client.streamChat(app.settings.serverUrl, app.settings.apiKey, buildRequest(system, tools, base, turn, mode, prepared.think)) { content, reasoning ->
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
                    if (tc.name == "project_note") projectChanged = true
                    if (tc.name == "set_reminder" || tc.name == "cancel_reminder") remindersChanged = true
                    status = (if (onServer) ServerTools.displayName(tc.name) else PhoneTools.displayName(tc.name)) + "…"
                    val argsJson = try {
                        JSONObject(tc.arguments.ifBlank { "{}" })
                    } catch (_: Exception) {
                        JSONObject()
                    }
                    argsJson.remove("approved")   // only the user can approve, never the model
                    // Project tools act on this chat's project
                    if (tc.name in PROJECT_TOOLS && projectId.isNotEmpty()) argsJson.put("project_id", projectId)
                    if (tc.name == "search_chats") argsJson.put("current_conversation", convId)
                    val needsOk = onServer && ServerTools.needsApproval(tc.name)
                    val outcome = if (needsOk && !askApproval(tc.name, argsJson)) {
                        val why = if (app.isVisible) "The user declined this action." else
                            "This needs the user's approval on screen; ask them to open the app and try again."
                        PhoneTools.Outcome("✗ Not approved: ${ServerTools.displayName(tc.name).lowercase()}",
                            JSONObject().put("ok", false).put("error", why).toString())
                    } else if (onServer) {
                        if (needsOk) argsJson.put("approved", true)
                        // Keep big results (web pages, files) within the space left in Stella's context
                        val left = prepared.info?.let { it.limit - it.used - turn.sumOf { t -> t.toString().length } / 3 }
                        argsJson.put("_max_chars", if (left == null) 12000 else maxOf(1500, (left * 3 * 0.6).toInt()))
                        withContext(Dispatchers.IO) { ServerTools.execute(app, tc.name, argsJson.toString()) }
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
            if (projectChanged) app.scope.launch { app.projects.refresh() }
            if (remindersChanged) app.scope.launch { app.reminders.refresh() }
            if (gen == generation) updateMeter()
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
                Role.ERROR, Role.SUMMARY -> {}
            }
        }
        flush()
        return out
    }

    /** Fallback for older servers without token counting: keep the last messages by count. */
    private fun trimByCount(out: List<JSONObject>): List<JSONObject> {
        if (out.size <= MAX_HISTORY) return out
        var start = out.size - MAX_HISTORY
        while (start < out.size && out[start].optString("role") != "user") start++
        return out.subList(start, out.size)
    }

    // ---- Context window ----------------------------------------------------------------------
    private class Prepared(
        val system: String,
        val history: List<JSONObject>,
        val needsRefresh: Boolean,
        val info: ContextInfo?,
        val tools: JSONArray,
        val think: Boolean,
    )

    private fun textOf(o: JSONObject): String {
        val c = o.opt("content")
        if (c is JSONArray) {
            return (0 until c.length()).joinToString(" ") { i ->
                c.optJSONObject(i)?.let { if (it.optString("type") == "text") it.optString("text") else "" }.orEmpty()
            }.trim()
        }
        return c?.toString().orEmpty()
    }

    /**
     * Builds what Stella will see: instructions (with the project and any earlier summary) and as
     * much recent history as fits. The server counts tokens exactly and picks relevant project files.
     */
    private suspend fun prepare(
        convId: String,
        projectId: String,
        images: Int,
        mode: Mode = modeOf(convId),
        fromVoice: Boolean = false,
    ): Prepared = withContext(Dispatchers.IO) {
        val list = app.store.messages(convId).map { it.toUi() }
        val lastSummary = list.indexOfLast { it.role == Role.SUMMARY }
        val summary = if (lastSummary >= 0) list[lastSummary].text else ""
        val history = buildHistory(list.drop(lastSummary + 1))
        val lastText = list.lastOrNull { it.role == Role.USER }?.text?.replace(PHOTO_REF, "").orEmpty()
        val sticky = synchronized(stickyTools) { stickyTools[convId]?.toSet().orEmpty() }
        val phoneNames = phoneToolsFor(lastText, fromVoice)?.let { it + sticky }
        val phoneTools = toolDefinitions(projectId.isNotEmpty(), mode, selected = emptyList(), phone = phoneNames)
        val baseSystem = systemPrompt("", summary, mode, "")
        val query = list.filter { it.role == Role.USER }.takeLast(2)
            .joinToString("\n") { it.text.replace(PHOTO_REF, "") }
        val plan = try {
            HubApi.context(app.settings.serverUrl, app.settings.apiKey, JSONObject()
                .put("project_id", projectId)
                .put("query", query)
                .put("fixed", baseSystem + phoneTools.toString())
                .put("images", images)
                .put("mode", mode.id)
                .put("mode_tools", mode.tools)
                .put("split", true)
                .put("sticky", JSONArray(sticky.toList()))
                .put("thinking", thinkingPref(mode))
                .put("last", lastText)
                .put("messages", JSONArray().apply {
                    history.forEach { put(JSONObject().put("role", it.optString("role")).put("content", textOf(it))) }
                }))
        } catch (_: Exception) {
            null
        }
        if (plan == null) {
            return@withContext Prepared(systemPrompt("", summary, mode, app.memory.promptSection()),
                withTurnContext(trimByCount(history), ""), false, null,
                toolDefinitions(projectId.isNotEmpty(), mode, selected = null, phone = phoneNames),
                think = thinkingPref(mode) == "on")
        }
        // The server picks which of its tools fit this message (each tool costs context space)
        val chosen = plan.optJSONArray("tools")?.let { arr -> (0 until arr.length()).map { arr.optString(it) } }

        val nCtx = plan.optInt("n_ctx", 8192)
        val limit = nCtx - plan.optInt("reply_reserve", 1024)
        val projectTokens = plan.optInt("project_tokens")
        val used = plan.optInt("fixed_tokens") + projectTokens + plan.optInt("history_tokens")
        var start = plan.optInt("keep_from", 0).coerceIn(0, history.size)
        while (start < history.size && history[start].optString("role") != "user") start++
        if (start >= history.size) start = history.indexOfLast { it.optString("role") == "user" }.coerceAtLeast(0)
        val userTurns = history.count { it.optString("role") == "user" }
        // Newer servers keep the instructions stable (so the model server can reuse its work on them) and
        // send what changes per message (relevant memories, file excerpts) to attach to the newest message.
        val memory = if (plan.has("memory_context")) plan.optString("memory_context") else app.memory.promptSection()
        Prepared(
            system = systemPrompt(plan.optString("project_context"), summary, mode, memory),
            history = withTurnContext(history.subList(start, history.size), plan.optString("turn_context")),
            needsRefresh = userTurns >= 2 && (start > 0 || used > limit * REFRESH_AT),
            info = ContextInfo(used, limit, nCtx, projectTokens),
            tools = toolDefinitions(projectId.isNotEmpty(), mode, selected = chosen, phone = phoneNames),
            think = if (plan.has("think")) plan.optBoolean("think") else thinkingPref(mode) == "on",
        )
    }

    /**
     * Puts the current time and per-message context in front of the newest user message only. Keeping these
     * out of the instructions means the start of the prompt stays the same between messages, so the model
     * server doesn't have to re-read the whole chat each time (much faster on a CPU).
     */
    private fun withTurnContext(history: List<JSONObject>, extra: String): List<JSONObject> {
        val i = history.indexOfLast { it.optString("role") == "user" }
        if (i < 0) return history
        val now = ZonedDateTime.now().format(DateTimeFormatter.ofPattern("EEEE, yyyy-MM-dd HH:mm 'UTC'xxx (VV)", Locale.ENGLISH))
        val block = buildString {
            append("[Automatic context, not written by the user]\nCurrent time: ").append(now)
            if (extra.isNotBlank()) append("\n").append(extra.trim())
            append("\n[End of context]\n\n")
        }
        val msg = JSONObject(history[i].toString())
        when (val c = msg.opt("content")) {
            is JSONArray -> {
                val first = (0 until c.length()).firstOrNull { c.optJSONObject(it)?.optString("type") == "text" }
                if (first != null) c.getJSONObject(first).put("text", block + c.getJSONObject(first).optString("text"))
            }
            else -> msg.put("content", block + (c?.toString() ?: ""))
        }
        return history.toMutableList().also { it[i] = msg }
    }

    /**
     * Phone actions this message might need. Voice commands get all of them (they're usually actions);
     * typed messages only the ones whose keywords appear, since every tool description costs context.
     */
    private fun phoneToolsFor(text: String, fromVoice: Boolean): Set<String>? {
        if (fromVoice) return null
        val t = text.lowercase()
        val out = mutableSetOf<String>()
        for ((name, rx) in PHONE_TRIGGERS) if (rx.containsMatchIn(t)) out.add(name)
        return out
    }

    private fun rememberTools(convId: String, tools: JSONArray) {
        val names = (0 until tools.length()).mapNotNull { tools.optJSONObject(it)?.optJSONObject("function")?.optString("name") }
        synchronized(stickyTools) { stickyTools.getOrPut(convId) { mutableSetOf() }.addAll(names) }
    }

    /** Re-measures the open chat's context use (for the meter). */
    fun updateMeter() {
        val id = currentId ?: return
        if (app.settings.apiKey.isBlank()) return
        meterJob?.cancel()
        meterJob = app.scope.launch {
            val projectId = conversations.firstOrNull { it.id == id }?.projectId.orEmpty()
            val p = prepare(id, projectId, 0)
            if (id == currentId && !busy && p.info != null) context = p.info
        }
    }

    /**
     * Refresh: Stella summarizes the chat so far and continues from the summary, freeing her memory.
     * All messages stay visible and synced; only what's sent to the model shrinks.
     */
    fun refreshContext() {
        if (busy) return
        val convId = currentId ?: return
        stopRequested = false
        busy = true
        status = "Refreshing context…"
        val gen = generation
        job = app.scope.launch {
            try {
                if (!compact(convId, keepUserTurns = 0) && contextNotice == null) {
                    contextNotice = "Nothing new to summarize yet."
                }
            } finally {
                if (gen == generation) {
                    busy = false
                    status = ""
                    updateMeter()
                }
            }
        }
    }

    /**
     * Summarizes everything since the last summary, except the last [keepUserTurns] exchanges, and
     * stores the summary as a message placed just before the part that's kept (synced like any message).
     */
    private suspend fun compact(convId: String, keepUserTurns: Int): Boolean {
        val stored = withContext(Dispatchers.IO) { app.store.messages(convId) }
        val last = stored.indexOfLast { it.role == Role.SUMMARY }
        val after = stored.drop(last + 1)
        val userIdx = after.indices.filter { after[it].role == Role.USER }
        var k = minOf(keepUserTurns, userIdx.size)
        while (k > 1 && userIdx[userIdx.size - k] == 0) k--
        val cut = if (k == 0) after.size else userIdx[userIdx.size - k]
        val part = after.take(cut)
        if (part.none { it.role == Role.USER || it.role == Role.ASSISTANT }) return false
        val arr = JSONArray()
        part.forEach { m ->
            arr.put(JSONObject()
                .put("role", StoredMessage.roleName(m.role))
                .put("content", m.text.replace(PHOTO_REF, "[photo]").replace(DIAGRAM_REF, "[diagram]")))
        }
        val previous = if (last >= 0) stored[last].text else ""
        val summary = try {
            withContext(Dispatchers.IO) {
                HubApi.summarize(app.settings.serverUrl, app.settings.apiKey, previous, arr, modeOf(convId).style)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            contextNotice = "Couldn't refresh the context: ${friendlyError(e)}"
            return false
        }
        if (summary.isBlank()) return false
        val created = if (cut < after.size) {
            val next = after[cut].createdAt
            val prev = part.last().createdAt
            if (next - prev >= 2) (prev + next) / 2 else next - 1
        } else nextTs()
        val m = StoredMessage(UUID.randomUUID().toString(), convId, Role.SUMMARY, summary.trim(), "", created, nextTs())
        withContext(Dispatchers.IO) { app.store.saveMessage(m) }
        if (convId == currentId) loadMessages(convId)
        contextNotice = null
        app.sync.request()
        return true
    }

    /**
     * Phone tools plus server tools. [selected] = the server tools the server chose for this message
     * (empty = phone tools only, null = older server: all, filtered here by project and mode).
     */
    private fun toolDefinitions(inProject: Boolean, mode: Mode, selected: List<String>?, phone: Set<String>? = null): JSONArray =
        JSONArray().apply {
        val server = ServerTools.definitions()
        val serverPicked = mutableListOf<Any>()
        val serverNames = mutableSetOf<String>()
        for (i in 0 until server.length()) {
            val name = server.optJSONObject(i)?.optJSONObject("function")?.optString("name").orEmpty()
            val keep = if (selected != null) name in selected else
                !(!inProject && name in PROJECT_TOOLS) && !(mode.tools == "minimal" && name !in MINIMAL_SERVER_TOOLS)
            if (keep) {
                serverPicked.add(server.get(i))
                serverNames.add(name)
            }
        }
        val phoneFilter = phone
        val phoneDefs = PhoneTools.definitions()
        for (i in 0 until phoneDefs.length()) {
            val name = phoneDefs.optJSONObject(i)?.optJSONObject("function")?.optString("name").orEmpty()
            // The server's real web search replaces the phone's "open a search in the browser"
            if (name in serverNames || (name == "web_search" && ServerTools.has("web_search"))) continue
            if (mode.tools == "minimal" && name !in MINIMAL_PHONE_TOOLS) continue
            if (phoneFilter != null && name !in phoneFilter) continue
            put(phoneDefs.get(i))
        }
        serverPicked.forEach { put(it) }
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

    private fun buildRequest(
        system: String,
        tools: JSONArray,
        base: List<JSONObject>,
        turn: List<JSONObject>,
        mode: Mode,
        thinking: Boolean,
    ): JSONObject {
        val all = JSONArray()
        all.put(JSONObject().put("role", "system").put("content", system))
        base.forEach { all.put(it) }
        turn.forEach { all.put(it) }
        // Qwen's recommended sampling for Qwen3.5 (model card): precise for code, a presence penalty elsewhere
        // to avoid the repetition loops quantized models can fall into. A mode's creativity overrides temperature.
        val code = mode.id == "programmer" || mode.id == "electronics"
        val temperature = mode.temperature ?: if (thinking) (if (code) 0.6 else 1.0) else 0.7
        return JSONObject()
            .put("model", "local")
            .put("messages", all)
            .put("tools", tools)
            .put("chat_template_kwargs", JSONObject().put("enable_thinking", thinking))
            .put("temperature", temperature)
            .put("top_p", if (thinking) 0.95 else 0.8)
            .put("top_k", 20)
            .put("min_p", 0.0)
            .put("presence_penalty", if (code) 0.0 else 1.5)
    }

    /** "on"/"off"/"auto": the chat's mode decides first, then the Settings choice. */
    private fun thinkingPref(mode: Mode): String = when (mode.thinking) {
        "on", "off" -> mode.thinking
        else -> app.settings.thinkingMode
    }

    private fun systemPrompt(project: String, summary: String, mode: Mode, memory: String): String {
        val name = app.settings.assistantName.ifBlank { "Stella" }
        return """
            You are $name, the user's private AI assistant (StellAI), running on their own computer. Warm and capable with a light space-explorer touch; concise and practical.
            The user is on their Android phone. To do things on it, call the tools instead of explaining how. Confirm briefly afterwards; never claim success if a tool failed.
            Markdown is fine; always put code in fenced blocks with a language; avoid tables.
            Use the calculate/electronics tools for any maths. Use render_diagram when a picture helps (it shows automatically; if it fails, fix it and retry once).
            Describe photos only from what you actually see.
            Save lasting facts with remember. Reminders: set_reminder (rings on all their phones); set_alarm is only for wake-up alarms. Lists: add_to_list. Current info: web_search then fetch_page, citing links.
            A user message may start with an automatic context block (time, memories, file excerpts); the user didn't write it.
        """.trimIndent() +
            (if (mode.prompt.isBlank()) "" else "\n\n## Current mode: ${mode.name}\n${mode.prompt}\n" +
                "These mode instructions take priority over the general style guidance above (such as keeping replies short).") +
            (if (memory.isBlank()) "" else "\n\n$memory") +
            (if (project.isBlank()) "" else "\n\n$project") +
            (if (summary.isBlank()) "" else "\n\n## Earlier in this chat\n" +
                "The start of this conversation was summarized to free up memory. Continue naturally from it; " +
                "the user can still see the full chat.\n$summary")
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
        val DIAGRAM_REF = Regex("!\\[[^\\]]*\\]\\(diagram:[0-9a-f]+\\)")
        private const val MAX_TOOL_ROUNDS = 6
        private const val MAX_HISTORY = 24
        /** Refresh the context automatically once it is this full. */
        private const val REFRESH_AT = 0.85f
        val PROJECT_TOOLS = setOf("project_note", "project_search")
        private val PHONE_TRIGGERS = mapOf(
            "set_alarm" to Regex("alarm|wake me"),
            "set_timer" to Regex("timer|countdown|count down"),
            "open_app" to Regex("\\bopen\\b|launch|\\bstart\\b|\\bplay\\b|\\bapp\\b|spotify|youtube|camera"),
            "open_url" to Regex("https?://|website|\\burl\\b|\\blink\\b|go to "),
            "navigate_to" to Regex("navigat|direction|drive to|route|\\bmaps?\\b|take me|how do i get"),
            "draft_text_message" to Regex("\\btext\\b|\\bsms\\b|message (to|for)|send .* (a )?message|tell \\w+ that"),
            "dial_number" to Regex("\\bcall\\b|\\bdial\\b|phone (number|him|her|them)"),
            "create_calendar_event" to Regex("calendar|event|meeting|appointment|schedule"),
            "set_flashlight" to Regex("flash ?light|torch|\\blight\\b"),
            "set_media_volume" to Regex("volume|louder|quieter|\\bmute|turn (it )?(up|down)"),
            "get_battery_status" to Regex("battery|charg(e|ing) level|how much charge"),
        )
        /** Server tools kept in "minimal" modes such as Writer. */
        private val MINIMAL_SERVER_TOOLS = setOf("remember", "forget", "project_note", "project_search", "library_search",
            "set_reminder", "add_to_list", "web_search", "fetch_page")
        private val MINIMAL_PHONE_TOOLS = setOf("set_alarm", "set_timer", "open_app", "open_url")
        private const val STREAMING_PREFIX = "streaming-"
    }
}
