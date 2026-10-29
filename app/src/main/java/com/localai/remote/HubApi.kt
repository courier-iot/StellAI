package com.localai.remote

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
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
