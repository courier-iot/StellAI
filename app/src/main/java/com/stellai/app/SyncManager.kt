package com.stellai.app

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * Keeps this device's chats in step with the laptop. Pushes local changes, then
 * pulls everything changed since the last sync (a cursor the laptop hands out).
 */
class SyncManager(private val app: App) {

    var status by mutableStateOf("Not synced yet")
        private set
    var lastSuccess by mutableStateOf(0L)
        private set

    private val mutex = Mutex()
    @Volatile
    private var again = false

    fun request() {
        app.scope.launch { syncNow() }
    }

    /** Runs a sync on the main thread's scope; overlapping calls are merged. */
    suspend fun syncNow() {
        if (!mutex.tryLock()) {
            again = true
            return
        }
        try {
            do {
                again = false
                val changed = try {
                    withContext(Dispatchers.IO) { runSync() }.also {
                        lastSuccess = System.currentTimeMillis()
                        status = "Synced"
                    }
                } catch (e: Exception) {
                    status = "Sync failed: ${app.engine.friendlyError(e)}"
                    again = false
                    false
                }
                if (changed) app.engine.onRemoteChanges()
            } while (again)
        } finally {
            mutex.unlock()
        }
    }

    /** Blocking. Returns true if changes from other devices were applied. */
    private fun runSync(): Boolean {
        val settings = app.settings
        val store = app.store
        if (settings.apiKey.isBlank()) throw IllegalStateException("Set the server and API key first")

        var cursor = settings.syncCursor
        var serverId = settings.syncServerId
        var changed = false

        for (page in 0 until 100) {
            val convs = store.dirtyConversations()
            val msgs = store.dirtyMessages()
            val body = JSONObject()
                .put("since", cursor)
                .put("conversations", JSONArray().apply { convs.forEach { put(it.toJson()) } })
                .put("messages", JSONArray().apply { msgs.forEach { put(it.toJson()) } })
            val resp = HubApi.sync(settings.serverUrl, settings.apiKey, body)

            val sid = resp.getString("server_id")
            if (serverId.isNotEmpty() && sid != serverId) {
                // The laptop's chat database is new (reinstalled or reset): upload everything again.
                store.markAllDirty()
                cursor = 0
                serverId = sid
                settings.syncCursor = 0
                settings.syncServerId = sid
                continue
            }
            serverId = sid
            settings.syncServerId = sid

            convs.forEach { store.markConversationClean(it.id, it.updatedAt) }
            msgs.forEach { store.markMessageClean(it.id, it.updatedAt) }

            resp.optJSONArray("conversations")?.let { arr ->
                for (i in 0 until arr.length()) {
                    if (store.applyRemoteConversation(Conversation.fromJson(arr.getJSONObject(i)))) changed = true
                }
            }
            resp.optJSONArray("messages")?.let { arr ->
                for (i in 0 until arr.length()) {
                    val m = StoredMessage.fromJson(arr.getJSONObject(i)) ?: continue
                    if (store.applyRemoteMessage(m)) changed = true
                }
            }

            cursor = resp.getLong("cursor")
            settings.syncCursor = cursor
            val morePending = resp.optBoolean("more") || store.dirtyMessages().isNotEmpty()
            if (!morePending) break
        }
        return changed
    }
}
