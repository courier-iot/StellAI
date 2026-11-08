package com.stellai.app

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.URI
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit

/** Calls to the laptop hub (laptop_hub.py): speech-to-text and chat sync. Blocking: use off the main thread. */
object HubApi {
    const val PORT = 8092

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .build()

    /** Same host as the AI server, on the hub's port. */
    fun url(serverUrl: String): String {
        val uri = URI(serverUrl.trim())
        val scheme = uri.scheme ?: "http"
        val host = uri.host ?: throw IOException("Bad server address")
        val hostPart = if (host.contains(':')) "[$host]" else host
        return "$scheme://$hostPart:$PORT"
    }

    /** Sends 16 kHz mono 16-bit audio to Whisper on the laptop; returns the text. */
    fun transcribe(serverUrl: String, apiKey: String, pcm: ShortArray, sampleRate: Int = 16000): String {
        val bytes = ByteBuffer.allocate(pcm.size * 2).order(ByteOrder.LITTLE_ENDIAN)
            .also { it.asShortBuffer().put(pcm) }
            .array()
        val request = Request.Builder()
            .url("${url(serverUrl)}/transcribe")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .header("X-Sample-Rate", sampleRate.toString())
            .post(bytes.toRequestBody("application/octet-stream".toMediaType()))
            .build()
        return JSONObject(execute(request)).optString("text").trim()
    }

    fun sync(serverUrl: String, apiKey: String, body: JSONObject): JSONObject {
        val request = Request.Builder()
            .url("${url(serverUrl)}/sync")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()
        return JSONObject(execute(request))
    }

    /** Tools that run on the server, in OpenAI tool format, plus the names that need the user's approval. */
    fun tools(serverUrl: String, apiKey: String): JSONObject {
        val request = Request.Builder()
            .url("${url(serverUrl)}/tools")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .get()
            .build()
        return JSONObject(execute(request))
    }

    /** Runs one server tool. Returns {"result": {...}, "summary": "..."}. */
    fun callTool(serverUrl: String, apiKey: String, name: String, argsJson: String): JSONObject {
        val request = Request.Builder()
            .url("${url(serverUrl)}/tools/${java.net.URLEncoder.encode(name, "UTF-8")}")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .post(argsJson.ifBlank { "{}" }.toRequestBody("application/json".toMediaType()))
            .build()
        return JSONObject(execute(request))
    }

    /** PNG bytes of a rendered diagram. */
    fun diagram(serverUrl: String, apiKey: String, id: String): ByteArray {
        val request = Request.Builder()
            .url("${url(serverUrl)}/diagrams/$id.png")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .get()
            .build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
            return response.body?.bytes() ?: throw IOException("Empty image")
        }
    }

    // ---- Long-term memory -----------------------------------------------------------------
    private fun authed(serverUrl: String, apiKey: String, path: String) = Request.Builder()
        .url("${url(serverUrl)}$path")
        .header("Authorization", "Bearer ${apiKey.trim()}")

    fun memories(serverUrl: String, apiKey: String): JSONArray =
        JSONObject(execute(authed(serverUrl, apiKey, "/memories").get().build())).optJSONArray("memories") ?: JSONArray()

    fun addMemory(serverUrl: String, apiKey: String, text: String) {
        val body = JSONObject().put("text", text).toString().toRequestBody("application/json".toMediaType())
        execute(authed(serverUrl, apiKey, "/memories").post(body).build())
    }

    fun deleteMemory(serverUrl: String, apiKey: String, id: String) {
        execute(authed(serverUrl, apiKey, "/memories/$id").delete().build())
    }

    /** Asks the server to pick out facts worth remembering from recent messages. */
    fun learn(serverUrl: String, apiKey: String, messages: JSONArray): JSONObject {
        val body = JSONObject().put("messages", messages).toString().toRequestBody("application/json".toMediaType())
        return JSONObject(execute(authed(serverUrl, apiKey, "/memories/learn").post(body).build()))
    }

    // ---- Projects and context ---------------------------------------------------------------
    /** Longer timeouts: reading a big PDF or describing an image can take a while. */
    private val slow = http.newBuilder().readTimeout(5, TimeUnit.MINUTES).writeTimeout(2, TimeUnit.MINUTES).build()
    private val JSON = "application/json".toMediaType()

    fun projects(serverUrl: String, apiKey: String): JSONArray =
        JSONObject(execute(authed(serverUrl, apiKey, "/projects").get().build())).optJSONArray("projects") ?: JSONArray()

    /** Creates (no id) or updates a project. Returns the saved project. */
    fun saveProject(serverUrl: String, apiKey: String, project: JSONObject): JSONObject =
        JSONObject(execute(authed(serverUrl, apiKey, "/projects").post(project.toString().toRequestBody(JSON)).build()))

    fun deleteProject(serverUrl: String, apiKey: String, id: String) {
        execute(authed(serverUrl, apiKey, "/projects/$id").delete().build())
    }

    fun addProjectNote(serverUrl: String, apiKey: String, projectId: String, text: String) {
        val body = JSONObject().put("text", text).toString().toRequestBody(JSON)
        execute(authed(serverUrl, apiKey, "/projects/$projectId/notes").post(body).build())
    }

    fun deleteProjectNote(serverUrl: String, apiKey: String, projectId: String, noteId: String) {
        execute(authed(serverUrl, apiKey, "/projects/$projectId/notes/$noteId").delete().build())
    }

    fun uploadProjectFile(serverUrl: String, apiKey: String, projectId: String, name: String, bytes: ByteArray) {
        val q = java.net.URLEncoder.encode(name, "UTF-8")
        val request = authed(serverUrl, apiKey, "/projects/$projectId/files?name=$q")
            .put(bytes.toRequestBody("application/octet-stream".toMediaType())).build()
        execute(request, slow)
    }

    fun deleteProjectFile(serverUrl: String, apiKey: String, projectId: String, fileId: String) {
        execute(authed(serverUrl, apiKey, "/projects/$projectId/files/$fileId").delete().build())
    }

    /** The text Stella sees from a project file. */
    fun projectFileText(serverUrl: String, apiKey: String, projectId: String, fileId: String): String =
        execute(authed(serverUrl, apiKey, "/projects/$projectId/files/$fileId.txt").get().build())

    // ---- Reminders, lists, library, GitHub ------------------------------------------------
    private fun getJson(serverUrl: String, apiKey: String, path: String): JSONObject =
        JSONObject(execute(authed(serverUrl, apiKey, path).get().build()))

    private fun postJson(serverUrl: String, apiKey: String, path: String, body: JSONObject): JSONObject =
        JSONObject(execute(authed(serverUrl, apiKey, path).post(body.toString().toRequestBody(JSON)).build()))

    fun reminders(serverUrl: String, apiKey: String): JSONArray =
        getJson(serverUrl, apiKey, "/reminders").optJSONArray("reminders") ?: JSONArray()

    fun addReminder(serverUrl: String, apiKey: String, text: String, dueAt: Long, repeat: String) {
        postJson(serverUrl, apiKey, "/reminders", JSONObject().put("text", text).put("due_at", dueAt).put("repeat", repeat))
    }

    /** action: {"done": true} | {"snooze_minutes": 10} | {"cancel": true} */
    fun reminderAction(serverUrl: String, apiKey: String, id: String, action: JSONObject) {
        postJson(serverUrl, apiKey, "/reminders/$id", action)
    }

    fun lists(serverUrl: String, apiKey: String): JSONObject =
        getJson(serverUrl, apiKey, "/lists").optJSONObject("lists") ?: JSONObject()

    fun addToList(serverUrl: String, apiKey: String, list: String, item: String) {
        postJson(serverUrl, apiKey, "/lists/" + java.net.URLEncoder.encode(list, "UTF-8").replace("+", "%20"),
            JSONObject().put("items", JSONArray().put(item)))
    }

    fun setListItem(serverUrl: String, apiKey: String, id: String, done: Boolean) {
        postJson(serverUrl, apiKey, "/lists/items/$id", JSONObject().put("done", done))
    }

    fun deleteListItem(serverUrl: String, apiKey: String, id: String) {
        execute(authed(serverUrl, apiKey, "/lists/items/$id").delete().build())
    }

    fun clearList(serverUrl: String, apiKey: String, list: String) {
        postJson(serverUrl, apiKey, "/lists/" + java.net.URLEncoder.encode(list, "UTF-8").replace("+", "%20") + "/clear", JSONObject())
    }

    fun library(serverUrl: String, apiKey: String): JSONObject = getJson(serverUrl, apiKey, "/library")

    fun github(serverUrl: String, apiKey: String): JSONObject = getJson(serverUrl, apiKey, "/github")

    // ---- Server status, models, health check, backups, chat search --------------------------
    fun server(serverUrl: String, apiKey: String): JSONObject = getJson(serverUrl, apiKey, "/server")

    fun switchModel(serverUrl: String, apiKey: String, key: String): String =
        postJson(serverUrl, apiKey, "/server/model", JSONObject().put("key", key)).optString("message")

    fun downloadModel(serverUrl: String, apiKey: String, key: String): String =
        postJson(serverUrl, apiKey, "/server/download", JSONObject().put("key", key)).optString("message")

    fun doctor(serverUrl: String, apiKey: String): JSONObject =
        JSONObject(execute(authed(serverUrl, apiKey, "/doctor").get().build(), slow))

    fun backups(serverUrl: String, apiKey: String): JSONObject = getJson(serverUrl, apiKey, "/backups")

    fun backupNow(serverUrl: String, apiKey: String): JSONObject =
        JSONObject(execute(authed(serverUrl, apiKey, "/backups").post("{}".toRequestBody(JSON)).build(), slow))

    fun backupConfig(serverUrl: String, apiKey: String, copyTo: String, keep: Int): JSONObject =
        postJson(serverUrl, apiKey, "/backups/config", JSONObject().put("copy_to", copyTo).put("keep", keep))

    /** Earlier chats matching a question, by keywords and meaning. */
    fun searchChats(serverUrl: String, apiKey: String, query: String, exclude: String = ""): JSONArray {
        val q = java.net.URLEncoder.encode(query, "UTF-8")
        val ex = java.net.URLEncoder.encode(exclude, "UTF-8")
        return getJson(serverUrl, apiKey, "/search?q=$q&exclude=$ex").optJSONArray("results") ?: JSONArray()
    }

    /** Avatar screens show what Stella is doing on any device. */
    fun avatarEvent(serverUrl: String, apiKey: String, event: JSONObject) {
        postJson(serverUrl, apiKey, "/avatar/event", event)
    }

    /** Avatar settings; also tells whether the server is new enough to have an avatar screen at all. */
    fun avatarConfig(serverUrl: String, apiKey: String): JSONObject = getJson(serverUrl, apiKey, "/avatar/config")

    /** The avatar screen page on the server (key in the #fragment so it isn't logged). */
    fun avatarPageUrl(serverUrl: String, apiKey: String): String =
        url(serverUrl) + "/avatar?app=1&src=" + java.net.URLEncoder.encode("phone-" + android.os.Build.MODEL, "UTF-8") +
            "#key=" + java.net.URLEncoder.encode(apiKey.trim(), "UTF-8")

    fun saveGithub(serverUrl: String, apiKey: String, body: JSONObject): JSONObject = postJson(serverUrl, apiKey, "/github", body)

    fun modes(serverUrl: String, apiKey: String): JSONArray =
        JSONObject(execute(authed(serverUrl, apiKey, "/modes").get().build())).optJSONArray("modes") ?: JSONArray()

    fun saveMode(serverUrl: String, apiKey: String, mode: JSONObject) {
        execute(authed(serverUrl, apiKey, "/modes").post(mode.toString().toRequestBody(JSON)).build())
    }

    fun deleteMode(serverUrl: String, apiKey: String, id: String) {
        execute(authed(serverUrl, apiKey, "/modes/$id").delete().build())
    }

    /** Token budget for the next request: project context, counts and how much history fits. */
    fun context(serverUrl: String, apiKey: String, body: JSONObject): JSONObject =
        JSONObject(execute(authed(serverUrl, apiKey, "/context").post(body.toString().toRequestBody(JSON)).build()))

    /** Summarizes chat messages (plus an earlier summary) so the chat can continue with a fresh context. */
    fun summarize(serverUrl: String, apiKey: String, previous: String, messages: JSONArray, style: String = "work"): String {
        val body = JSONObject().put("previous", previous).put("messages", messages).put("style", style)
            .toString().toRequestBody(JSON)
        return JSONObject(execute(authed(serverUrl, apiKey, "/summarize").post(body).build(), slow)).optString("summary")
    }

    // ---- Photos and custom wake words ------------------------------------------------------
    fun uploadImage(serverUrl: String, apiKey: String, id: String, jpeg: ByteArray) {
        execute(authed(serverUrl, apiKey, "/images/$id.jpg").put(jpeg.toRequestBody("image/jpeg".toMediaType())).build())
    }

    /** Raw bytes of a server file such as /images/ID.jpg or /diagrams/ID.png. */
    fun download(serverUrl: String, apiKey: String, path: String): ByteArray {
        http.newCall(authed(serverUrl, apiKey, path).get().build()).execute().use { response ->
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
            return response.body?.bytes() ?: throw IOException("Empty file")
        }
    }

    /** Custom wake words on the server: list of (id, label). */
    fun wakeWords(serverUrl: String, apiKey: String): List<Pair<String, String>> {
        val arr = JSONObject(execute(authed(serverUrl, apiKey, "/wakewords").get().build())).optJSONArray("wakewords")
            ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            arr.optJSONObject(i)?.let { it.optString("id") to it.optString("label") }
        }.filter { it.first.isNotBlank() }
    }

    /** True if the hub (sync + voice) answers. */
    fun health(serverUrl: String): Boolean = try {
        val request = Request.Builder().url("${url(serverUrl)}/health").get().build()
        http.newCall(request).execute().use { it.isSuccessful }
    } catch (e: Exception) {
        false
    }

    private fun execute(request: Request, client: OkHttpClient = http): String =
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                val msg = try {
                    JSONObject(text).optString("error")
                } catch (e: Exception) {
                    ""
                }
                throw IOException("HTTP ${response.code}${if (msg.isNotBlank()) ": $msg" else ""}")
            }
            text
        }
}
