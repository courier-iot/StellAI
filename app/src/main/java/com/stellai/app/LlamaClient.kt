package com.stellai.app

import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Talks to llama-server's OpenAI-compatible API (/v1/chat/completions)
 * with streaming, and collects any tool calls the model makes.
 */
class LlamaClient {

    data class ToolCall(val id: String, val name: String, val arguments: String)
    data class StreamResult(val content: String, val reasoning: String, val toolCalls: List<ToolCall>)

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        // A slow laptop can take a while to read a long prompt before the first token.
        .readTimeout(5, TimeUnit.MINUTES)
        .build()

    @Volatile
    private var currentCall: Call? = null

    fun cancel() {
        currentCall?.cancel()
    }

    private fun Request.Builder.auth(apiKey: String): Request.Builder =
        if (apiKey.isNotBlank()) header("Authorization", "Bearer ${apiKey.trim()}") else this

    /** Quick check that the server is reachable and the key works. Returns the model name. */
    fun testConnection(baseUrl: String, apiKey: String): String {
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/v1/models")
            .auth(apiKey)
            .get()
            .build()
        http.newCall(request).execute().use { response ->
            val body = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw IOException("HTTP ${response.code}: ${errorMessage(body)}")
            }
            val data = JSONObject(body).optJSONArray("data")
            val id = data?.optJSONObject(0)?.optString("id").orEmpty()
            return id.substringAfterLast('/').ifBlank { "connected" }
        }
    }

    /**
     * Streams one chat completion. Blocking: call from a background thread.
     * [onUpdate] receives the full content and reasoning so far after each chunk.
     */
    fun streamChat(
        baseUrl: String,
        apiKey: String,
        body: JSONObject,
        onUpdate: (content: String, reasoning: String) -> Unit,
    ): StreamResult {
        body.put("stream", true)
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/v1/chat/completions")
            .auth(apiKey)
            .header("Accept", "text/event-stream")
            .post(body.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val content = StringBuilder()
        val reasoning = StringBuilder()
        val toolBuilders = sortedMapOf<Int, ToolCallBuilder>()

        val call = http.newCall(request)
        currentCall = call
        try {
            call.execute().use { response ->
                if (!response.isSuccessful) {
                    val err = response.body?.string().orEmpty()
                    throw IOException("HTTP ${response.code}: ${errorMessage(err)}")
                }
                val source = response.body?.source() ?: throw IOException("Empty response")
                while (true) {
                    val line = source.readUtf8Line() ?: break
                    if (!line.startsWith("data:")) continue
                    val data = line.removePrefix("data:").trim()
                    if (data.isEmpty()) continue
                    if (data == "[DONE]") break

                    val json = try {
                        JSONObject(data)
                    } catch (e: Exception) {
                        continue
                    }
                    json.optJSONObject("error")?.let {
                        throw IOException(it.optString("message", "Server error"))
                    }
                    val choice = json.optJSONArray("choices")?.optJSONObject(0) ?: continue
                    val delta = choice.optJSONObject("delta") ?: continue

                    var changed = false
                    val text = delta.optStringOrNull("content")
                    if (!text.isNullOrEmpty()) {
                        content.append(text); changed = true
                    }
                    val think = delta.optStringOrNull("reasoning_content")
                    if (!think.isNullOrEmpty()) {
                        reasoning.append(think); changed = true
                    }
                    delta.optJSONArray("tool_calls")?.let { calls ->
                        for (i in 0 until calls.length()) {
                            val tc = calls.optJSONObject(i) ?: continue
                            val index = tc.optInt("index", i)
                            val b = toolBuilders.getOrPut(index) { ToolCallBuilder() }
                            tc.optStringOrNull("id")?.let { v -> if (v.isNotEmpty() && b.id.isEmpty()) { b.id = v } }
                            tc.optJSONObject("function")?.let { fn ->
                                fn.optStringOrNull("name")?.let { v -> if (v.isNotEmpty() && b.name.isEmpty()) { b.name = v } }
                                fn.optStringOrNull("arguments")?.let { b.arguments.append(it) }
                            }
                        }
                    }
                    if (changed) onUpdate(content.toString(), reasoning.toString())
                }
            }
        } finally {
            currentCall = null
        }

        val calls = toolBuilders.values
            .filter { it.name.isNotEmpty() }
            .mapIndexed { i, b ->
                ToolCall(
                    id = b.id.ifEmpty { "call_$i" },
                    name = b.name,
                    arguments = b.arguments.toString().ifBlank { "{}" },
                )
            }
        return StreamResult(content.toString(), reasoning.toString(), calls)
    }

    private class ToolCallBuilder {
        var id = ""
        var name = ""
        val arguments = StringBuilder()
    }

    private fun errorMessage(body: String): String = try {
        JSONObject(body).optJSONObject("error")?.optString("message") ?: body.take(200)
    } catch (e: Exception) {
        body.take(200).ifBlank { "no details" }
    }
}

/** org.json returns "null" for JSON nulls with optString; this returns real null instead. */
fun JSONObject.optStringOrNull(key: String): String? =
    if (!has(key) || isNull(key)) null else optString(key)

fun jsonArrayOf(vararg items: Any): JSONArray = JSONArray().apply { items.forEach { put(it) } }
