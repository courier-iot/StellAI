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

    /** Tools that run on the server (calculator, electronics, diagrams), in OpenAI tool format. */
    fun tools(serverUrl: String, apiKey: String): JSONArray {
        val request = Request.Builder()
            .url("${url(serverUrl)}/tools")
            .header("Authorization", "Bearer ${apiKey.trim()}")
            .get()
            .build()
        return JSONObject(execute(request)).optJSONArray("tools") ?: JSONArray()
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

    private fun execute(request: Request): String =
        http.newCall(request).execute().use { response ->
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
