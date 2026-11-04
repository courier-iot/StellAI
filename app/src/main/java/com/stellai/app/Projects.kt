package com.stellai.app

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

data class ProjectNote(val id: String, val text: String, val source: String)

data class ProjectFile(val id: String, val name: String, val kind: String, val size: Long, val tokens: Int, val note: String)

data class Project(
    val id: String,
    val name: String,
    val color: String,
    val instructions: String,
    val notes: List<ProjectNote>,
    val files: List<ProjectFile>,
) {
    companion object {
        fun fromJson(o: JSONObject): Project {
            val notes = o.optJSONArray("notes") ?: JSONArray()
            val files = o.optJSONArray("files") ?: JSONArray()
            return Project(
                id = o.optString("id"),
                name = o.optString("name"),
                color = o.optString("color", "cyan"),
                instructions = o.optString("instructions"),
                notes = (0 until notes.length()).mapNotNull { i ->
                    notes.optJSONObject(i)?.let { ProjectNote(it.optString("id"), it.optString("text"), it.optString("source")) }
                },
                files = (0 until files.length()).mapNotNull { i ->
                    files.optJSONObject(i)?.let {
                        ProjectFile(it.optString("id"), it.optString("name"), it.optString("kind"), it.optLong("size"),
                            it.optInt("tokens"), it.optString("note"))
                    }
                },
            )
        }
    }
}

/**
 * Projects group chats and give them shared instructions, notes and files. They live on the
 * server (so every device and the server's own tools share them); this keeps a copy for the UI.
 */
class ProjectManager(private val app: App) {

    val items = mutableStateListOf<Project>()
    var status by mutableStateOf("")
    /** Name of the file being uploaded, or null. */
    var uploading by mutableStateOf<String?>(null)
        private set

    init {
        try {
            items.addAll(parse(JSONArray(app.settings.projectCache)))
        } catch (_: Exception) {
        }
    }

    private fun parse(arr: JSONArray) = (0 until arr.length()).mapNotNull { i ->
        arr.optJSONObject(i)?.let { Project.fromJson(it) }
    }.filter { it.id.isNotBlank() }

    fun get(id: String?): Project? = if (id.isNullOrEmpty()) null else items.firstOrNull { it.id == id }

    private val url get() = app.settings.serverUrl
    private val key get() = app.settings.apiKey

    /** Fetches the latest projects. Returns false if the server couldn't be reached. */
    suspend fun refresh(): Boolean = try {
        val arr = withContext(Dispatchers.IO) { HubApi.projects(url, key) }
        items.clear()
        items.addAll(parse(arr))
        app.settings.projectCache = arr.toString()
        status = ""
        true
    } catch (e: Exception) {
        status = "Couldn't reach the server: ${app.engine.friendlyError(e)}"
        false
    }

    private suspend fun attempt(what: String, block: suspend () -> Unit): Boolean = try {
        block()
        refresh()
        true
    } catch (e: Exception) {
        status = "Couldn't $what: ${app.engine.friendlyError(e)}"
        false
    }

    /** Creates (id = null) or updates a project; returns its id. */
    suspend fun save(id: String?, name: String, color: String, instructions: String): String? {
        var saved: String? = null
        attempt("save the project") {
            val body = JSONObject().put("name", name.trim()).put("color", color).put("instructions", instructions.trim())
            if (id != null) body.put("id", id)
            saved = withContext(Dispatchers.IO) { HubApi.saveProject(url, key, body) }.optString("id")
        }
        return saved
    }

    suspend fun delete(id: String) {
        if (attempt("delete the project") { withContext(Dispatchers.IO) { HubApi.deleteProject(url, key, id) } }) {
            app.sync.request() // its chats leave the project on every device
        }
    }

    suspend fun addNote(projectId: String, text: String) {
        if (text.isBlank()) return
        attempt("save the note") { withContext(Dispatchers.IO) { HubApi.addProjectNote(url, key, projectId, text.trim()) } }
    }

    suspend fun deleteNote(projectId: String, noteId: String) {
        attempt("delete the note") { withContext(Dispatchers.IO) { HubApi.deleteProjectNote(url, key, projectId, noteId) } }
    }

    suspend fun deleteFile(projectId: String, fileId: String) {
        attempt("delete the file") { withContext(Dispatchers.IO) { HubApi.deleteProjectFile(url, key, projectId, fileId) } }
    }

    /** Uploads files picked on the phone. The server extracts their text (PDF, Word, code, images). */
    suspend fun addFiles(context: Context, projectId: String, uris: List<Uri>) {
        for (uri in uris) {
            val name = displayName(context, uri)
            uploading = name
            try {
                val bytes = withContext(Dispatchers.IO) { readLimited(context, uri) }
                withContext(Dispatchers.IO) { HubApi.uploadProjectFile(url, key, projectId, name, bytes) }
                status = ""
            } catch (e: Exception) {
                status = "Couldn't add $name: ${app.engine.friendlyError(e).removePrefix("HTTP 400: ")}"
            }
        }
        uploading = null
        refresh()
    }

    suspend fun fileText(projectId: String, fileId: String): String =
        withContext(Dispatchers.IO) { HubApi.projectFileText(url, key, projectId, fileId) }

    private fun displayName(context: Context, uri: Uri): String {
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0)?.let { if (it.isNotBlank()) return it }
        }
        return uri.lastPathSegment?.substringAfterLast('/') ?: "file"
    }

    private fun readLimited(context: Context, uri: Uri): ByteArray {
        val input = context.contentResolver.openInputStream(uri) ?: throw IllegalStateException("can't open the file")
        input.use {
            val out = java.io.ByteArrayOutputStream()
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = it.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
                if (out.size() > MAX_FILE) throw IllegalStateException("files must be under 25 MB")
            }
            return out.toByteArray()
        }
    }

    companion object {
        const val MAX_FILE = 25_000_000
    }
}
