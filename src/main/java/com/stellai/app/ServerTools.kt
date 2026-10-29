package com.stellai.app

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Tools that run on the StellAI server: exact calculator, electronics helpers and diagram
 * rendering. The list comes from the server, so new server tools appear without an app update.
 */
object ServerTools {

    @Volatile
    private var defs = JSONArray()
    @Volatile
    private var names: Set<String> = emptySet()
    @Volatile
    private var fetchedAt = 0L
    @Volatile
    private var fetchedFor = ""

    fun definitions(): JSONArray = defs

    fun has(name: String) = name in names

    /** Blocking. Refreshes the tool list at most every 10 minutes (or when the server changes). */
    fun refresh(app: App) {
        val s = app.settings
        val key = "${s.serverUrl}|${s.apiKey}"
        val fresh = System.currentTimeMillis() - fetchedAt < 10 * 60_000
        if (fresh && key == fetchedFor && names.isNotEmpty()) return
        try {
            val arr = HubApi.tools(s.serverUrl, s.apiKey)
            val set = mutableSetOf<String>()
            for (i in 0 until arr.length()) {
                arr.optJSONObject(i)?.optJSONObject("function")?.optString("name")?.let { if (it.isNotEmpty()) set.add(it) }
            }
            defs = arr
            names = set
            fetchedAt = System.currentTimeMillis()
            fetchedFor = key
        } catch (_: Exception) {
            // Older server or unreachable: carry on with phone tools only.
            if (key != fetchedFor) {
                defs = JSONArray()
                names = emptySet()
            }
        }
    }

    fun displayName(name: String): String = when (name) {
        "render_diagram" -> "Drawing diagram"
        else -> "Calculating"
    }

    /** Blocking. Runs a server tool and turns the answer into a chat line plus a result for the model. */
    fun execute(app: App, name: String, argsJson: String): PhoneTools.Outcome {
        return try {
            val resp = HubApi.callTool(app.settings.serverUrl, app.settings.apiKey, name, argsJson)
            val result = resp.optJSONObject("result") ?: JSONObject()
            val summary = resp.optString("summary").ifBlank { name }
            val ok = result.optBoolean("ok", false)
            val image = result.optString("image")
            val line = StringBuilder(if (ok) "✓ " else "✗ ").append(summary)
            if (ok && image.isNotBlank()) {
                val title = result.optString("title").ifBlank { "Diagram" }.replace("]", ")")
                line.append("\n![").append(title).append("](diagram:").append(image).append(")")
            }
            PhoneTools.Outcome(line.toString(), result.toString())
        } catch (e: Exception) {
            val msg = app.engine.friendlyError(e)
            PhoneTools.Outcome("✗ $msg", JSONObject().put("ok", false).put("error", msg).toString())
        }
    }
}

/**
 * Images shown in chats: server-rendered diagrams ("diagram:ID") and photos ("image:ID").
 * Kept in memory and on disk; fetched from the server when another device sent them.
 */
object MediaCache {
    private val memory = LruCache<String, Bitmap>(16)
    private val ID = Regex("[0-9a-f]{16}")

    private fun parse(ref: String): Pair<String, String> {
        val kind = ref.substringBefore(':')
        val id = ref.substringAfter(':')
        require((kind == "diagram" || kind == "image") && ID.matches(id)) { "bad image reference" }
        return kind to id
    }

    private fun file(app: App, kind: String, id: String): File {
        val dir = File(app.cacheDir, kind).apply { mkdirs() }
        return File(dir, if (kind == "image") "$id.jpg" else "$id.png")
    }

    fun peek(ref: String): Bitmap? = memory.get(ref)

    /** Blocking: the raw file, from disk or the server. */
    fun bytes(app: App, ref: String): ByteArray {
        val (kind, id) = parse(ref)
        val f = file(app, kind, id)
        if (f.exists()) return f.readBytes()
        val path = if (kind == "image") "/images/$id.jpg" else "/diagrams/$id.png"
        return HubApi.download(app.settings.serverUrl, app.settings.apiKey, path).also { f.writeBytes(it) }
    }

    /** Blocking. */
    fun load(app: App, ref: String): Bitmap {
        memory.get(ref)?.let { return it }
        val b = bytes(app, ref)
        val bmp = BitmapFactory.decodeByteArray(b, 0, b.size) ?: throw IllegalStateException("not an image")
        memory.put(ref, bmp)
        return bmp
    }

    /** Stores a new photo taken on this phone. */
    fun saveLocal(app: App, id: String, jpeg: ByteArray) {
        file(app, "image", id).writeBytes(jpeg)
    }
}
