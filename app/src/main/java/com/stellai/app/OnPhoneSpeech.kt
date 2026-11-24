package com.stellai.app

import android.app.ActivityManager
import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * Speech to text on the phone (English), with whisper.cpp's whisper-cli, for when the server can't be reached.
 * The model (31–190 MB) is downloaded from Hugging Face (ggerganov/whisper.cpp) on Wi-Fi, or mobile data if allowed.
 */
class OnPhoneSpeech(private val app: App) {

    /** A speech model size. [bytes]: about how big the download is. */
    data class Size(val id: String, val label: String, val file: String, val bytes: Long, val detail: String)

    companion object {
        val SIZES = listOf(
            Size("tiny", "Fast", "ggml-tiny.en-q5_1.bin", 32_000_000, "quickest, makes more mistakes"),
            Size("base", "Balanced", "ggml-base.en-q5_1.bin", 60_000_000, "good for commands and short messages"),
            Size("small", "Accurate", "ggml-small.en-q5_1.bin", 190_000_000, "best, a few seconds slower"),
        )
        private const val REPO = "ggerganov/whisper.cpp"
    }

    private val prefs = app.getSharedPreferences("onphone_speech", Context.MODE_PRIVATE)
    private val http = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS).build()

    /** The size on the phone, or null. */
    var installed by mutableStateOf(prefs.getString("installed", null)?.let { id ->
        SIZES.firstOrNull { it.id == id }?.takeIf { modelFile(it).isFile }
    })
        private set
    var download by mutableStateOf<DownloadState?>(null)
        private set
    private var downloading: Size? = null
    private var job: Job? = null

    private fun dir() = File(app.filesDir, "speech").apply { mkdirs() }
    private fun modelFile(s: Size) = File(dir(), s.file)

    private fun program(name: String) = File(app.applicationInfo.nativeLibraryDir, name)
    val hasProgram: Boolean get() = program("libwhisper_cli.so").isFile

    val ready: Boolean get() = hasProgram && installed != null

    /** Fast for most phones; the smallest on phones with under 5 GB of memory (they're slower, too). */
    fun recommended(): Size {
        val mi = ActivityManager.MemoryInfo()
        (app.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).getMemoryInfo(mi)
        return if (mi.totalMem < 5_000_000_000L) SIZES[0] else SIZES[1]
    }

    // ---- Downloading ----------------------------------------------------------------------------------------
    fun startDownload(size: Size = recommended(), confirmedMobile: Boolean = false) {
        if (job?.isActive == true) return
        downloading = size
        mobileOk = confirmedMobile
        download = DownloadState(null, "Speech (${size.label})", partFile(size).length(), size.bytes, "Starting…", true)
        job = app.scope.launch { runDownload(size) }
    }

    private var mobileOk = false

    private fun partFile(s: Size) = File(dir(), s.file + ".part")

    fun cancelDownload(discard: Boolean) {
        job?.cancel()
        job = null
        val s = downloading
        if (discard) {
            s?.let { partFile(it).delete() }
            download = null
        } else {
            download = download?.copy(running = false, status = "Paused")
        }
    }

    fun resumeDownload(confirmedMobile: Boolean) {
        startDownload(downloading ?: recommended(), confirmedMobile)
    }

    private fun mayDownload() = !app.onPhone.onMobileData || app.settings.onPhoneMobileData || mobileOk

    private suspend fun runDownload(size: Size) {
        val part = partFile(size)
        fun show(status: String, running: Boolean = true, error: String = "", total: Long = size.bytes) {
            download = DownloadState(null, "Speech (${size.label})", part.length(), total, status, running, error)
        }
        try {
            if (!mayDownload()) {
                show("Paused: waiting for Wi-Fi (or allow mobile data)", running = false)
                return
            }
            // its exact size and checksum, from Hugging Face's file list (download anyway if that fails)
            val (total, sha) = withContext(Dispatchers.IO) { fileInfo(size.file) }
            val expected = if (total > 0) total else size.bytes
            var attempts = 0
            while (true) {
                try {
                    withContext(Dispatchers.IO) {
                        fetch(part, "https://huggingface.co/$REPO/resolve/main/${size.file}", total) {
                            show("Downloading…", total = expected)
                        }
                    }
                    break
                } catch (e: kotlinx.coroutines.CancellationException) {
                    throw e
                } catch (e: WaitForWifi) {
                    show("Paused: waiting for Wi-Fi (or allow mobile data)", running = false, total = expected)
                    return
                } catch (e: Exception) {
                    if (++attempts >= 6) {
                        show("Stopped", running = false, error = "Download failed: ${e.message ?: e.javaClass.simpleName}. Tap Resume.", total = expected)
                        return
                    }
                    show("Connection lost, retrying ($attempts of 5)…", total = expected)
                    delay(3000L * attempts)
                }
            }
            if (sha.isNotEmpty()) {
                show("Checking the file…", total = expected)
                if (!withContext(Dispatchers.IO) { sha256(part) }.equals(sha, ignoreCase = true)) {
                    part.delete()
                    show("Stopped", running = false, error = "The download was damaged and has been deleted. Tap Resume.", total = expected)
                    return
                }
            }
            val final = modelFile(size)
            final.delete()
            if (!part.renameTo(final)) throw IOException("Couldn't save the speech model")
            val old = installed
            installed = size
            prefs.edit().putString("installed", size.id).apply()
            if (old != null && old != size) modelFile(old).delete()
            download = null
            downloading = null
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            show("Stopped", running = false, error = "Download failed: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    private class WaitForWifi : IOException("waiting for Wi-Fi")

    /** Size and SHA-256 of a model file on Hugging Face ((-1, "") if the list can't be read). */
    private fun fileInfo(name: String): Pair<Long, String> {
        try {
            val req = Request.Builder().url("https://huggingface.co/api/models/$REPO/tree/main").header("User-Agent", "StellAI").build()
            http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return -1L to ""
                val arr = JSONArray(r.body?.string().orEmpty())
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    if (o.optString("path") == name) {
                        val lfs = o.optJSONObject("lfs")
                        return (lfs?.optLong("size") ?: o.optLong("size")) to lfs?.optString("oid").orEmpty()
                    }
                }
            }
        } catch (_: Exception) {
        }
        return -1L to ""
    }

    private suspend fun fetch(part: File, url: String, total: Long, progress: () -> Unit) {
        val from = part.length()
        if (total > 0 && from >= total) return
        val req = Request.Builder().url(url).header("User-Agent", "StellAI")
            .apply { if (from > 0) header("Range", "bytes=$from-") }.build()
        val job = kotlin.coroutines.coroutineContext[Job]
        http.newCall(req).execute().use { r ->
            if (r.code == 416) return
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            RandomAccessFile(part, "rw").use { out ->
                if (r.code == 206) out.seek(from) else out.setLength(0)
                val src = r.body?.byteStream() ?: throw IOException("Empty response")
                val buf = ByteArray(256 * 1024)
                var last = 0L
                var sinceCheck = 0L
                while (true) {
                    if (job?.isActive == false) throw kotlinx.coroutines.CancellationException()
                    val n = src.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    sinceCheck += n
                    val now = System.currentTimeMillis()
                    if (now - last > 400) {
                        last = now
                        withContext(Dispatchers.Main) { progress() }
                    }
                    if (sinceCheck > 8L * 1024 * 1024) {
                        sinceCheck = 0
                        if (!mayDownload()) throw WaitForWifi()
                    }
                }
            }
        }
        if (total > 0 && part.length() < total) throw IOException("The download ended early")
    }

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { input ->
            val buf = ByteArray(1024 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    fun remove() {
        val s = installed ?: return
        installed = null
        prefs.edit().remove("installed").apply()
        modelFile(s).delete()
    }

    // ---- Turning speech into text -------------------------------------------------------------------------------
    /** The faster program needs newer processor instructions; once it fails on this phone, the other one is used. */
    private fun pickProgram(): File {
        val fast = program("libwhisper_cli_fast.so")
        if (fast.isFile && !prefs.getBoolean("fast_failed", false) && cpuHasFastInstructions()) return fast
        return program("libwhisper_cli.so")
    }

    private fun cpuHasFastInstructions(): Boolean = try {
        val features = File("/proc/cpuinfo").readLines().firstOrNull { it.startsWith("Features") }.orEmpty().split(' ', '\t')
        "asimddp" in features && "asimdhp" in features
    } catch (_: Exception) {
        false
    }

    /**
     * Who turns speech into text: Settings → Speech: "server", "phone", or "auto" (the phone when the server can't be
     * reached or fails, and there's a speech model on it). Blocking: call off the main thread.
     */
    fun transcribe(pcm: ShortArray, rate: Int = 16000): String {
        val s = app.settings
        val noServer = s.serverUrl.isBlank() || s.apiKey.isBlank()
        return when (s.speechSource) {
            "phone" -> onPhone(pcm, rate)
            "server" -> HubApi.transcribe(s.serverUrl, s.apiKey, pcm, rate)
            else -> when {
                !ready -> if (noServer) throw IOException(
                    "There's no server, and no speech model on the phone yet (Settings → On-phone brain → Speech)."
                ) else try {
                    HubApi.transcribe(s.serverUrl, s.apiKey, pcm, rate)
                } catch (e: Exception) {
                    throw IOException((e.message ?: "The server can't be reached") +
                        ". Tip: download on-phone speech in Settings → On-phone brain to talk without the server.")
                }
                noServer || !HubApi.reachable(s.serverUrl) -> onPhone(pcm, rate)
                else -> try {
                    HubApi.transcribe(s.serverUrl, s.apiKey, pcm, rate)
                } catch (_: Exception) {
                    onPhone(pcm, rate)
                }
            }
        }
    }

    /** Speech to text with whisper-cli on this phone. */
    fun onPhone(pcm: ShortArray, rate: Int = 16000): String {
        val size = installed ?: throw IOException("No speech model on the phone yet (Settings → On-phone brain → Speech).")
        if (!hasProgram) throw IOException("This build of StellAI doesn't include on-phone speech.")
        val work = File(app.cacheDir, "speech").apply { mkdirs() }
        val wav = File(work, "said.wav")
        writeWav(wav, pcm, rate)
        val exe = pickProgram()
        return try {
            runCli(exe, size, wav, work)
        } catch (e: CrashedOnInstructions) {
            // the faster program doesn't suit this processor after all: use the other one from now on
            prefs.edit().putBoolean("fast_failed", true).apply()
            runCli(program("libwhisper_cli.so"), size, wav, work)
        } finally {
            wav.delete()
        }
    }

    private class CrashedOnInstructions : IOException("illegal instruction")

    private fun runCli(exe: File, size: Size, wav: File, work: File): String {
        val outBase = File(work, "said")
        val outTxt = File(work, "said.txt")
        outTxt.delete()
        val cores = Runtime.getRuntime().availableProcessors()
        val threads = if (cores >= 8) 4 else maxOf(2, cores / 2)
        val cmd = listOf(exe.path, "-m", modelFile(size).path, "-f", wav.path, "-l", "en", "-t", threads.toString(),
            "-nt", "-np", "-otxt", "-of", outBase.path)
        val pb = ProcessBuilder(cmd).redirectErrorStream(true).directory(work)
        pb.environment()["LD_LIBRARY_PATH"] = app.applicationInfo.nativeLibraryDir
        val p = pb.start()
        val output = StringBuilder()
        val reader = Thread {
            try {
                p.inputStream.bufferedReader().forEachLine { if (output.length < 4000) output.appendLine(it) }
            } catch (_: Exception) {
            }
        }.apply { isDaemon = true; start() }
        if (!p.waitFor(60, TimeUnit.SECONDS)) {
            p.destroyForcibly()
            throw IOException("On-phone speech took too long.")
        }
        reader.join(500)
        val code = p.exitValue()
        if (code == 128 + 4 && exe.name.contains("fast")) throw CrashedOnInstructions()
        if (code != 0) {
            val why = output.lines().lastOrNull { it.contains("error", true) }?.trim()
                ?: if (code == 128 + 9) "Android stopped it (memory)" else "exit code $code"
            throw IOException("On-phone speech failed: $why")
        }
        val text = if (outTxt.isFile) outTxt.readText() else ""
        outTxt.delete()
        return cleanTranscript(text)
    }

    /** Joins the lines and drops Whisper's sound notes: "[BLANK_AUDIO]", "(music)", "[ Silence ]". */
    private fun cleanTranscript(text: String): String = text
        .replace(Regex("\\[[^\\]]*\\]|\\((?:[^)]*(?:music|silence|blank|noise|inaudible|applause|laugh)[^)]*)\\)", RegexOption.IGNORE_CASE), " ")
        .replace(Regex("\\s+"), " ")
        .trim()

    private fun writeWav(f: File, pcm: ShortArray, rate: Int) {
        val data = pcm.size * 2
        val b = ByteBuffer.allocate(44 + data).order(ByteOrder.LITTLE_ENDIAN)
        b.put("RIFF".toByteArray()).putInt(36 + data).put("WAVE".toByteArray())
        b.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(rate).putInt(rate * 2).putShort(2).putShort(16)
        b.put("data".toByteArray()).putInt(data)
        b.asShortBuffer().put(pcm)
        f.writeBytes(b.array())
    }
}
