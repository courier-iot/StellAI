package com.stellai.app

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONObject

data class Conversation(
    val id: String,
    val title: String,
    val createdAt: Long,
    val updatedAt: Long,
    val deleted: Boolean = false,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("title", title)
        .put("created_at", createdAt).put("updated_at", updatedAt)
        .put("deleted", deleted)

    companion object {
        fun fromJson(o: JSONObject) = Conversation(
            id = o.getString("id"),
            title = o.optString("title"),
            createdAt = o.optLong("created_at"),
            updatedAt = o.getLong("updated_at"),
            deleted = o.optBoolean("deleted"),
        )
    }
}

/** A chat message as stored and synced. Errors are never stored. */
data class StoredMessage(
    val id: String,
    val convId: String,
    val role: Role,
    val text: String,
    val reasoning: String,
    val createdAt: Long,
    val updatedAt: Long,
    val deleted: Boolean = false,
) {
    fun toUi() = UiMessage(id, role, text, reasoning)

    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("conv_id", convId).put("role", roleName(role))
        .put("text", text).put("reasoning", reasoning)
        .put("created_at", createdAt).put("updated_at", updatedAt)
        .put("deleted", deleted)

    companion object {
        fun roleName(r: Role) = when (r) {
            Role.USER -> "user"
            Role.ASSISTANT -> "assistant"
            else -> "tool"
        }

        fun parseRole(s: String) = when (s) {
            "user" -> Role.USER
            "assistant" -> Role.ASSISTANT
            "tool" -> Role.TOOL
            else -> null
        }

        fun fromJson(o: JSONObject): StoredMessage? {
            val role = parseRole(o.optString("role")) ?: return null
            return StoredMessage(
                id = o.getString("id"),
                convId = o.getString("conv_id"),
                role = role,
                text = o.optString("text"),
                reasoning = o.optString("reasoning"),
                createdAt = o.optLong("created_at"),
                updatedAt = o.getLong("updated_at"),
                deleted = o.optBoolean("deleted"),
            )
        }
    }
}

/**
 * Local copy of all chats. Rows changed on this device are marked "dirty"
 * until the laptop has accepted them.
 */
class ChatStore(context: Context) : SQLiteOpenHelper(context, "chats.db", null, 2) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE conversations(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, dirty INTEGER NOT NULL DEFAULT 1)"""
        )
        db.execSQL(
            """CREATE TABLE messages(
                id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                reasoning TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                dirty INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0)"""
        )
        db.execSQL("CREATE INDEX msg_conv ON messages(conv_id, created_at)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) db.execSQL("ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    }

    // ---- Reads -------------------------------------------------------------------------
    fun conversations(): List<Conversation> =
        readableDatabase.rawQuery(
            "SELECT id, title, created_at, updated_at, deleted FROM conversations WHERE deleted=0 ORDER BY updated_at DESC",
            null,
        ).use { c -> c.map { conv(it) } }

    fun messages(convId: String): List<StoredMessage> =
        readableDatabase.rawQuery(
            "SELECT id, conv_id, role, text, reasoning, created_at, updated_at, deleted FROM messages WHERE conv_id=? AND deleted=0 ORDER BY created_at, id",
            arrayOf(convId),
        ).use { c -> c.mapNotNull { msg(it) } }

    fun dirtyConversations(): List<Conversation> =
        readableDatabase.rawQuery(
            "SELECT id, title, created_at, updated_at, deleted FROM conversations WHERE dirty=1", null,
        ).use { c -> c.map { conv(it) } }

    fun dirtyMessages(): List<StoredMessage> =
        readableDatabase.rawQuery(
            "SELECT id, conv_id, role, text, reasoning, created_at, updated_at, deleted FROM messages WHERE dirty=1 LIMIT 2000", null,
        ).use { c -> c.mapNotNull { msg(it) } }

    private fun conversationUpdatedAt(id: String): Long? =
        readableDatabase.rawQuery("SELECT updated_at FROM conversations WHERE id=?", arrayOf(id))
            .use { if (it.moveToFirst()) it.getLong(0) else null }

    private fun isDeleted(convId: String): Boolean =
        readableDatabase.rawQuery("SELECT 1 FROM conversations WHERE id=? AND deleted=1", arrayOf(convId))
            .use { it.moveToFirst() }

    private fun messageUpdatedAt(id: String): Long? =
        readableDatabase.rawQuery("SELECT updated_at FROM messages WHERE id=?", arrayOf(id))
            .use { if (it.moveToFirst()) it.getLong(0) else null }

    // ---- Local writes (marked dirty for sync) ------------------------------------------
    fun saveConversation(c: Conversation, dirty: Boolean = true) {
        writableDatabase.insertWithOnConflict("conversations", null, ContentValues().apply {
            put("id", c.id); put("title", c.title); put("created_at", c.createdAt)
            put("updated_at", c.updatedAt); put("deleted", if (c.deleted) 1 else 0); put("dirty", if (dirty) 1 else 0)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun saveMessage(m: StoredMessage, dirty: Boolean = true) {
        writableDatabase.insertWithOnConflict("messages", null, ContentValues().apply {
            put("id", m.id); put("conv_id", m.convId); put("role", StoredMessage.roleName(m.role))
            put("text", m.text); put("reasoning", m.reasoning); put("created_at", m.createdAt)
            put("updated_at", m.updatedAt); put("dirty", if (dirty) 1 else 0); put("deleted", if (m.deleted) 1 else 0)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun touchConversation(id: String, now: Long) {
        writableDatabase.execSQL("UPDATE conversations SET updated_at=?, dirty=1 WHERE id=?", arrayOf<Any>(now, id))
    }

    fun deleteConversation(id: String, now: Long) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL("UPDATE conversations SET deleted=1, updated_at=?, dirty=1 WHERE id=?", arrayOf<Any>(now, id))
            db.delete("messages", "conv_id=?", arrayOf(id))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    // ---- Sync ----------------------------------------------------------------------------
    /** Clears the dirty flag only if the row hasn't changed again since it was sent. */
    fun markConversationClean(id: String, updatedAt: Long) {
        writableDatabase.execSQL("UPDATE conversations SET dirty=0 WHERE id=? AND updated_at=?", arrayOf<Any>(id, updatedAt))
    }

    fun markMessageClean(id: String, updatedAt: Long) {
        writableDatabase.execSQL("UPDATE messages SET dirty=0 WHERE id=? AND updated_at=?", arrayOf<Any>(id, updatedAt))
    }

    fun markAllDirty() {
        writableDatabase.execSQL("UPDATE conversations SET dirty=1")
        writableDatabase.execSQL("UPDATE messages SET dirty=1")
    }

    /** Applies a change from another device. Returns true if anything changed here. */
    fun applyRemoteConversation(c: Conversation): Boolean {
        val local = conversationUpdatedAt(c.id)
        if (local != null && local >= c.updatedAt) return false
        val db = writableDatabase
        db.beginTransaction()
        try {
            saveConversation(c, dirty = false)
            if (c.deleted) db.delete("messages", "conv_id=?", arrayOf(c.id))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
        return true
    }

    fun applyRemoteMessage(m: StoredMessage): Boolean {
        if (isDeleted(m.convId)) return false
        val local = messageUpdatedAt(m.id)
        if (local != null && local >= m.updatedAt) return false
        saveMessage(m, dirty = false)
        return true
    }

    // ---- Cursor helpers --------------------------------------------------------------------
    private fun conv(c: Cursor) = Conversation(c.getString(0), c.getString(1), c.getLong(2), c.getLong(3), c.getInt(4) == 1)

    private fun msg(c: Cursor): StoredMessage? {
        val role = StoredMessage.parseRole(c.getString(2)) ?: return null
        return StoredMessage(c.getString(0), c.getString(1), role, c.getString(3), c.getString(4), c.getLong(5), c.getLong(6),
            c.getInt(7) == 1)
    }

    /** Marks messages as deleted (synced to other devices). */
    fun deleteMessages(ids: List<String>, now: Long) {
        if (ids.isEmpty()) return
        val db = writableDatabase
        db.beginTransaction()
        try {
            for (id in ids) {
                db.execSQL("UPDATE messages SET deleted=1, text='', reasoning='', updated_at=?, dirty=1 WHERE id=?", arrayOf<Any>(now, id))
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    private inline fun <T> Cursor.map(f: (Cursor) -> T): List<T> {
        val out = ArrayList<T>(count)
        while (moveToNext()) out.add(f(this))
        return out
    }

    private inline fun <T : Any> Cursor.mapNotNull(f: (Cursor) -> T?): List<T> {
        val out = ArrayList<T>(count)
        while (moveToNext()) f(this)?.let { out.add(it) }
        return out
    }
}
