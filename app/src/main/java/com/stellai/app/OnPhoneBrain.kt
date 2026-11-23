package com.stellai.app

import android.app.ActivityManager
import android.content.Context
import android.net.ConnectivityManager
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
import android.os.StatFs
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.net.ServerSocket
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

/** The model on this phone, as saved after a download or a manual load. */
data class InstalledModel(
    val repo: String,          // "unsloth/Qwen3-1.7B-GGUF", or "" for a file loaded by hand
    val file: String,          // file name
    val path: String,          // where it is on the phone
    val size: Long,
    val params: Double,
    val createdAt: String,
    val downloads: Long,
    val manual: Boolean,
) {
    val name: String get() = if (repo.isNotEmpty()) ModelPicker.displayName(repo) else file.removeSuffix(".gguf")

    fun toJson(): JSONObject = JSONObject().put("repo", repo).put("file", file).put("path", path).put("size", size)
        .put("params", params).put("created_at", createdAt).put("downloads", downloads).put("manual", manual)

    companion object {
        fun fromJson(o: JSONObject) = InstalledModel(o.optString("repo"), o.optString("file"), o.optString("path"),
            o.optLong("size"), o.optDouble("params", 0.0), o.optString("created_at"), o.optLong("downloads"),
            o.optBoolean("manual"))
    }
}

/** A download in progress (or paused / failed). */
data class DownloadState(
    val choice: ModelPicker.Choice?,
    val label: String,
    val done: Long,
    val total: Long,
    val status: String,
    val running: Boolean,
    val error: String = "",
) {
    val fraction: Float get() = if (total <= 0) 0f else (done.toFloat() / total).coerceIn(0f, 1f)
}

/**
 * Stella's on-phone brain: a small model run by llama.cpp's llama-server on this phone, for when the server can't be
 * reached (or always, if you choose). Finds a model that suits the phone on Hugging Face, downloads it (resuming,
 * checked), and starts and stops the brain program. All Compose state is changed on the main thread.
 */
class OnPhoneBrain(private val app: App) {

    private val prefs = app.getSharedPreferences("onphone", Context.MODE_PRIVATE)
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    var installed by mutableStateOf(prefs.getString("installed", null)?.let {
        try { InstalledModel.fromJson(JSONObject(it)).takeIf { m -> File(m.path).isFile } } catch (_: Exception) { null }
    })
        private set
    var download by mutableStateOf<DownloadState?>(null)
        private set
    /** A model to suggest: the first-run recommendation, or a better one found later. Shown as a dialog. */
    var offer by mutableStateOf<ModelPicker.Choice?>(null)
        private set
    /** True when [offer] is an upgrade over the installed model (not the first one). */
    var offerIsUpgrade by mutableStateOf(false)
        private set
    var searching by mutableStateOf(false)
        private set
    var searchNote by mutableStateOf("")
        private set
    /** "off", "starting", "ready" or "failed". */
    var engineState by mutableStateOf("off")
        private set
    var engineError by mutableStateOf("")
        private set

    // ---- The phone ---------------------------------------------------------------------------------------
    val device: ModelPicker.Device
        get() {
            val mi = ActivityManager.MemoryInfo()
            (app.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager).getMemoryInfo(mi)
            val free = try { StatFs(app.filesDir.path).availableBytes } catch (_: Exception) { 0L }
            return ModelPicker.Device(mi.totalMem, free + (installed?.size ?: 0L),
                Build.SUPPORTED_64_BIT_ABIS.contains("arm64-v8a"), Build.VERSION.SDK_INT)
        }

    val budget: ModelPicker.Budget? get() = ModelPicker.budget(device)

    /** The brain program, unpacked by Android at install (missing in builds made without it). */
    private fun programFile() = File(app.applicationInfo.nativeLibraryDir, "libllama_server.so")

    val hasProgram: Boolean get() = programFile().isFile

    fun deviceSummary(): String {
        val d = device
        return "Memory ${ModelPicker.sizeText(d.totalRam)}, free storage ${ModelPicker.sizeText(d.freeStorage)}" +
            (budget?.let { " — ${it.label}: up to ${ModelPicker.sizeText(it.maxFile)}" } ?: "")
    }

    private fun isMetered(): Boolean = try {
        (app.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager).isActiveNetworkMetered
    } catch (_: Exception) {
        true
    }

    /** May a download run right now? (Wi-Fi, or mobile data when allowed or confirmed for this download.) */
    fun mayDownloadNow(confirmedMobile: Boolean = false): Boolean =
        !isMetered() || app.settings.onPhoneMobileData || confirmedMobile

    val onMobileData: Boolean get() = isMetered()

    // ---- Finding a model -----------------------------------------------------------------------------------
    /**
     * Searches Hugging Face (trusted uploaders only) for the best model this phone can run. Returns null if none
     * fits or there's no connection. Blocking: call off the main thread.
     */
    fun findBest(b: ModelPicker.Budget): ModelPicker.Choice? {
        val found = mutableListOf<ModelPicker.Candidate>()
        var reached = false
        for (author in ModelPicker.TRUSTED_AUTHORS) {
            try {
                val arr = JSONArray(get("https://huggingface.co/api/models?author=$author&filter=gguf&sort=downloads&direction=-1&limit=60"))
                reached = true
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val id = o.optString("id", o.optString("modelId"))
                    if (id.isEmpty()) continue
                    found.add(ModelPicker.Candidate(id, ModelPicker.paramsFromName(id), o.optLong("downloads"),
                        o.optLong("likes"), o.optString("createdAt"), gatedOf(o)))
                }
            } catch (_: Exception) {
            }
        }
        if (!reached) throw IOException("Couldn't reach Hugging Face. Check the internet connection.")
        val today = java.time.LocalDate.now().toEpochDay()
        val shortlist = ModelPicker.filter(found, b)
            .sortedByDescending { c ->
                ModelPicker.score(if (c.params > 0) c.params else b.maxParams * 0.6, c.createdAt, c.downloads, b, today)
            }.take(10)
        val choices = shortlist.mapNotNull { c ->
            val file = try { ModelPicker.pickFile(files(c.repo), b) } catch (_: Exception) { null } ?: return@mapNotNull null
            val params = ModelPicker.paramsOf(c, file)
            if (params > b.maxParams * 1.15) return@mapNotNull null
            ModelPicker.Choice(c, file, params, ModelPicker.score(params, c.createdAt, c.downloads, b, today))
        }
        return choices.maxByOrNull { it.score }
    }

    private fun gatedOf(o: JSONObject): Boolean {
        if (!o.has("gated") || o.isNull("gated")) return false
        return when (val g = o.opt("gated")) {
            is Boolean -> g
            else -> g.toString().let { it.isNotEmpty() && it != "false" }
        }
    }

    /** The files of a Hugging Face repository (top folder), with sizes and checksums. */
    private fun files(repo: String): List<ModelPicker.RepoFile> {
        val arr = JSONArray(get("https://huggingface.co/api/models/$repo/tree/main"))
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            if (o.optString("type") != "file") return@mapNotNull null
            val lfs = o.optJSONObject("lfs")
            ModelPicker.RepoFile(o.optString("path"), lfs?.optLong("size") ?: o.optLong("size"), lfs?.optString("oid").orEmpty())
        }
    }

    private fun repoInfo(repo: String): ModelPicker.Candidate = try {
        val o = JSONObject(get("https://huggingface.co/api/models/$repo"))
        ModelPicker.Candidate(repo, ModelPicker.paramsFromName(repo), o.optLong("downloads"), o.optLong("likes"),
            o.optString("createdAt"), gatedOf(o))
    } catch (_: Exception) {
        ModelPicker.Candidate(repo, ModelPicker.paramsFromName(repo), 0, 0, "")
    }

    /**
     * A model from a pasted Hugging Face link: a file link (…/resolve/main/x.gguf or …/blob/main/x.gguf), a
     * repository link, or just "owner/repo" (then the best file for this phone is picked).
     */
    fun fromLink(text: String): ModelPicker.Choice {
        val t = text.trim().removePrefix("https://").removePrefix("http://").removePrefix("huggingface.co/")
            .removePrefix("hf.co/").substringBefore('?').trimEnd('/')
        val parts = t.split('/')
        if (parts.size < 2) throw IOException("That doesn't look like a Hugging Face link.")
        val repo = parts[0] + "/" + parts[1]
        val b = budget ?: throw IOException(ModelPicker.whyNot(device))
        val all = files(repo)
        val c = repoInfo(repo)
        if (c.gated) throw IOException("That model needs a Hugging Face account to download (it's gated).")
        val file = if (parts.size >= 5 && (parts[2] == "resolve" || parts[2] == "blob")) {
            val path = parts.drop(4).joinToString("/")
            all.firstOrNull { it.path == path } ?: throw IOException("Couldn't find $path in $repo.")
        } else {
            // a repository: its best file for this phone (allowing any size that fits in storage)
            val roomy = b.copy(maxFile = device.freeStorage - 1_000_000_000L)
            ModelPicker.pickFile(all, b) ?: ModelPicker.pickFile(all, roomy)
                ?: throw IOException("No single-file GGUF model in $repo fits this phone.")
        }
        if (!file.path.endsWith(".gguf", ignoreCase = true)) throw IOException("That isn't a GGUF model file.")
        val params = ModelPicker.paramsOf(c, file)
        return ModelPicker.Choice(c, file, params,
            ModelPicker.score(params, c.createdAt, c.downloads, b, java.time.LocalDate.now().toEpochDay()))
    }

    private fun get(url: String): String {
        val req = Request.Builder().url(url).header("User-Agent", "StellAI").get().build()
        http.newCall(req).execute().use { r ->
            val body = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            return body
        }
    }

    /** The score of the model on the phone (a hand-loaded one counts as a year old). */
    private fun installedScore(b: ModelPicker.Budget): Double {
        val m = installed ?: return -1.0
        val today = java.time.LocalDate.now().toEpochDay()
        val created = m.createdAt.ifEmpty { java.time.LocalDate.ofEpochDay(today - 365).toString() }
        return ModelPicker.score(m.params, created, m.downloads, b, today)
    }

    /** Settings → "Find the best model now": searches and offers what it finds (even if it's the same one). */
    fun searchNow() {
        if (searching) return
        val b = budget ?: run { searchNote = ModelPicker.whyNot(device); return }
        searching = true
        searchNote = "Searching Hugging Face…"
        app.scope.launch {
            try {
                val best = withContext(Dispatchers.IO) { findBest(b) }
                prefs.edit().putLong("last_check", System.currentTimeMillis()).apply()
                when {
                    best == null -> searchNote = "No model that suits this phone was found."
                    installed?.repo.equals(best.candidate.repo, ignoreCase = true) &&
                        installed?.file == best.file.path.substringAfterLast('/') ->
                        searchNote = "You already have the best model for this phone (${best.candidate.let { ModelPicker.displayName(it.repo) }})."
                    else -> {
                        searchNote = ""
                        offerIsUpgrade = installed != null
                        offer = best
                    }
                }
            } catch (e: Exception) {
                searchNote = "✗ " + (e.message ?: "Search failed")
            } finally {
                searching = false
            }
        }
    }

    /**
     * On app start: the first time, recommends a model for this phone (once); later, looks for a better one about
     * once a week when allowed (Wi-Fi, or mobile data if allowed). Quiet on any problem.
     */
    private var started = false

    fun onAppStart() {
        if (started) return
        started = true
        resumeIfInterrupted()
        val b = budget ?: return
        val s = app.settings
        val now = System.currentTimeMillis()
        if (installed == null && download == null && !s.onPhoneAsked) {
            if (!hasProgram) return
            app.scope.launch {
                val best = try { withContext(Dispatchers.IO) { findBest(b) } } catch (_: Exception) { null } ?: return@launch
                if (installed == null && !s.onPhoneAsked) {
                    offerIsUpgrade = false
                    offer = best
                }
            }
            return
        }
        if (installed == null || !s.onPhoneAutoFind || isMetered() && !s.onPhoneMobileData) return
        if (now - prefs.getLong("last_check", 0L) < 7L * 24 * 3600_000) return
        prefs.edit().putLong("last_check", now).apply()
        app.scope.launch {
            val best = try { withContext(Dispatchers.IO) { findBest(b) } } catch (_: Exception) { null } ?: return@launch
            if (ModelPicker.isUpgrade(best, installed?.repo.orEmpty(), installedScore(b), dismissed())) {
                offerIsUpgrade = true
                offer = best
            }
        }
    }

    /** Shows [c] in the download dialog (a model from a pasted link). */
    fun offerChoice(c: ModelPicker.Choice) {
        offerIsUpgrade = installed != null
        offer = c
    }

    /** Back in the app: a download that was waiting for Wi-Fi carries on once it's allowed. */
    fun onAppVisible() {
        val d = download ?: return resumeIfInterrupted()
        if (!d.running && d.error.isEmpty() && d.status.startsWith("Paused: waiting") && mayDownloadNow()) resumeDownload(false)
    }

    private fun dismissed(): Set<String> = prefs.getStringSet("dismissed", emptySet()).orEmpty()

    /** "Not now" / "Don't suggest this one": an upgrade that's turned down isn't offered again. */
    fun dismissOffer(forever: Boolean) {
        val o = offer ?: return
        if (!offerIsUpgrade) app.settings.onPhoneAsked = true
        if (forever && offerIsUpgrade) prefs.edit().putStringSet("dismissed", dismissed() + o.candidate.repo).apply()
        offer = null
    }

    fun acceptOffer(confirmedMobile: Boolean) {
        val o = offer ?: return
        app.settings.onPhoneAsked = true
        offer = null
        startDownload(o, confirmedMobile)
    }

    // ---- Downloading ---------------------------------------------------------------------------------------
    private var downloadJob: Job? = null

    private fun modelDir() = File(app.filesDir, "models").apply { mkdirs() }

    private fun safeName(name: String) = name.substringAfterLast('/').replace(Regex("[^A-Za-z0-9._-]"), "_")

    /** Starts (or resumes) downloading [choice]. Stays on Wi-Fi unless mobile data is allowed or confirmed. */
    fun startDownload(choice: ModelPicker.Choice, confirmedMobile: Boolean = false) {
        if (downloadJob?.isActive == true) return
        if (confirmedMobile) allowMobileFor(choice)
        val label = ModelPicker.displayName(choice.candidate.repo)
        prefs.edit().putString("pending", JSONObject()
            .put("repo", choice.candidate.repo).put("path", choice.file.path).put("size", choice.file.size)
            .put("sha", choice.file.sha256).put("params", choice.params).put("created_at", choice.candidate.createdAt)
            .put("downloads", choice.candidate.downloads).put("score", choice.score).toString()).apply()
        download = DownloadState(choice, label, partFile(choice).length(), choice.file.size, "Starting…", true)
        downloadJob = app.scope.launch { runDownload(choice, label, confirmedMobile) }
    }

    private fun partFile(c: ModelPicker.Choice) = File(modelDir(), safeName(c.file.path) + ".part")

    /** A download that was stopped (app closed, phone restarted) carries on, if the network allows. */
    private fun resumeIfInterrupted() {
        if (downloadJob?.isActive == true) return
        val p = prefs.getString("pending", null) ?: return
        val o = try { JSONObject(p) } catch (_: Exception) { return }
        val c = ModelPicker.Candidate(o.optString("repo"), o.optDouble("params", 0.0), o.optLong("downloads"), 0,
            o.optString("created_at"))
        val choice = ModelPicker.Choice(c, ModelPicker.RepoFile(o.optString("path"), o.optLong("size"), o.optString("sha")),
            o.optDouble("params", 0.0), o.optDouble("score", 0.0))
        if (mayDownloadNow()) startDownload(choice)
        else download = DownloadState(choice, ModelPicker.displayName(c.repo), partFile(choice).length(), choice.file.size,
            "Paused: waiting for Wi-Fi", false)
    }

    fun cancelDownload(discard: Boolean) {
        downloadJob?.cancel()
        downloadJob = null
        val d = download
        if (discard) {
            d?.choice?.let { partFile(it).delete() }
            prefs.edit().remove("pending").apply()
            download = null
        } else if (d != null) {
            download = d.copy(running = false, status = "Paused")
        }
    }

    fun resumeDownload(confirmedMobile: Boolean) {
        download?.choice?.let { startDownload(it, confirmedMobile) }
    }

    private suspend fun runDownload(choice: ModelPicker.Choice, label: String, confirmedMobile: Boolean) {
        val part = partFile(choice)
        val total = choice.file.size
        val wake = (app.getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "StellAI:model-download").apply { setReferenceCounted(false) }
        @Suppress("DEPRECATION")
        val wifi = (app.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "StellAI:model-download").apply { setReferenceCounted(false) }
        fun show(status: String, running: Boolean = true, error: String = "") {
            download = DownloadState(choice, label, part.length(), total, status, running, error)
        }
        try {
            wake.acquire(3 * 3600_000L)
            wifi.acquire()
            var attempts = 0
            while (true) {
                if (!mayDownloadNow(confirmedMobile)) {
                    show("Paused: waiting for Wi-Fi (or allow mobile data)", running = false)
                    return
                }
                try {
                    withContext(Dispatchers.IO) { fetchInto(part, choice, total) { show("Downloading…") } }
                    break
                } catch (e: PausedForWifi) {
                    show("Paused: waiting for Wi-Fi (or allow mobile data)", running = false)
                    return
                } catch (e: kotlinx.coroutines.CancellationException) {
                    throw e
                } catch (e: Exception) {
                    attempts++
                    if (attempts >= 6) {
                        show("Stopped", running = false, error = "Download failed: ${e.message ?: e.javaClass.simpleName}. Tap Resume to carry on.")
                        return
                    }
                    show("Connection lost, retrying ($attempts of 5)…")
                    delay(3000L * attempts)
                }
            }
            if (choice.file.sha256.isNotEmpty()) {
                show("Checking the file…")
                val sum = withContext(Dispatchers.IO) { sha256(part) }
                if (!sum.equals(choice.file.sha256, ignoreCase = true)) {
                    part.delete()
                    show("Stopped", running = false, error = "The downloaded file was damaged and has been deleted. Tap Resume to download it again.")
                    return
                }
            }
            val final = File(modelDir(), safeName(choice.file.path))
            final.delete()
            if (!part.renameTo(final)) throw IOException("Couldn't save the model")
            install(InstalledModel(choice.candidate.repo, final.name, final.path, final.length(), choice.params,
                choice.candidate.createdAt, choice.candidate.downloads, manual = false))
            prefs.edit().remove("pending").apply()
            download = null
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            show("Stopped", running = false, error = "Download failed: ${e.message ?: e.javaClass.simpleName}")
        } finally {
            if (wake.isHeld) wake.release()
            if (wifi.isHeld) wifi.release()
        }
    }

    private class PausedForWifi : IOException("paused")

    /** Downloads the rest of the file into [part] (resuming from its size). */
    private suspend fun fetchInto(part: File, choice: ModelPicker.Choice, total: Long, progress: () -> Unit) {
        val from = part.length()
        if (total > 0 && from >= total) return
        val url = "https://huggingface.co/${choice.candidate.repo}/resolve/main/" +
            choice.file.path.split('/').joinToString("/") { Uri.encode(it) }
        val req = Request.Builder().url(url).header("User-Agent", "StellAI")
            .apply { if (from > 0) header("Range", "bytes=$from-") }.get().build()
        val call = http.newBuilder().readTimeout(60, TimeUnit.SECONDS).build().newCall(req)
        val job = kotlin.coroutines.coroutineContext[Job]
        call.execute().use { r ->
            if (r.code == 416) return
            if (r.code == 401 || r.code == 403) throw IOException("Hugging Face refused the download (HTTP ${r.code}): the model may need an account.")
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            val append = r.code == 206
            RandomAccessFile(part, "rw").use { out ->
                if (append) out.seek(from) else out.setLength(0)
                val src = r.body?.byteStream() ?: throw IOException("Empty response")
                val buf = ByteArray(256 * 1024)
                var lastShown = 0L
                var sinceCheck = 0L
                while (true) {
                    if (job?.isActive == false) throw kotlinx.coroutines.CancellationException()
                    val n = src.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    sinceCheck += n
                    val now = System.currentTimeMillis()
                    if (now - lastShown > 400) {
                        lastShown = now
                        withContext(Dispatchers.Main) { progress() }
                    }
                    // Wi-Fi dropped and the phone moved to mobile data: pause rather than use it
                    if (sinceCheck > 16L * 1024 * 1024) {
                        sinceCheck = 0
                        if (isMetered() && !app.settings.onPhoneMobileData && !confirmedMobileFor(choice)) throw PausedForWifi()
                    }
                }
            }
        }
        if (total > 0 && part.length() < total) throw IOException("The download ended early")
    }

    /** Mobile data was OK'd for this download in its confirm dialog (kept until it finishes). */
    private var mobileOkRepo = ""
    private fun confirmedMobileFor(c: ModelPicker.Choice) = mobileOkRepo == c.candidate.repo + c.file.path

    fun allowMobileFor(c: ModelPicker.Choice) {
        mobileOkRepo = c.candidate.repo + c.file.path
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

    /** Makes [m] the brain, deleting the previous model file. */
    private suspend fun install(m: InstalledModel) {
        val old = installed
        stopEngine()
        installed = m
        prefs.edit().putString("installed", m.toJson().toString()).apply()
        if (old != null && old.path != m.path) withContext(Dispatchers.IO) { File(old.path).delete() }
    }

    /** Settings → Remove: deletes the model from the phone. */
    fun remove() {
        val m = installed ?: return
        stopEngine()
        installed = null
        prefs.edit().remove("installed").apply()
        app.scope.launch(Dispatchers.IO) { File(m.path).delete() }
    }

    // ---- Loading a model file by hand ----------------------------------------------------------------------
    var importing by mutableStateOf("")
        private set

    /** Copies a .gguf file picked with the phone's file picker into the app and makes it the brain. */
    fun importFile(uri: Uri) {
        if (importing.isNotEmpty()) return
        importing = "Copying…"
        app.scope.launch {
            try {
                val m = withContext(Dispatchers.IO) { copyIn(uri) }
                install(m)
                importing = ""
                searchNote = "✓ Loaded ${m.name}"
            } catch (e: Exception) {
                importing = ""
                searchNote = "✗ " + (e.message ?: "Couldn't load that file")
            }
        }
    }

    private suspend fun copyIn(uri: Uri): InstalledModel {
        val cr = app.contentResolver
        var name = "model.gguf"
        var size = -1L
        cr.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val ni = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                val si = c.getColumnIndex(android.provider.OpenableColumns.SIZE)
                if (ni >= 0) name = c.getString(ni) ?: name
                if (si >= 0 && !c.isNull(si)) size = c.getLong(si)
            }
        }
        val input = cr.openInputStream(uri) ?: throw IOException("Couldn't open that file")
        val target = File(modelDir(), "manual-" + safeName(name).let { if (it.endsWith(".gguf", true)) it else "$it.gguf" })
        val tmp = File(target.path + ".part")
        input.use { src ->
            val magic = ByteArray(4)
            if (src.read(magic) != 4 || String(magic, Charsets.US_ASCII) != "GGUF") {
                throw IOException("That isn't a GGUF model file (llama.cpp format).")
            }
            val free = StatFs(app.filesDir.path).availableBytes
            if (size > 0 && size > free - 500_000_000L) throw IOException("Not enough free storage to copy it (${ModelPicker.sizeText(size)}).")
            tmp.outputStream().use { out ->
                out.write(magic)
                val buf = ByteArray(1024 * 1024)
                var copied = 4L
                var last = 0L
                while (true) {
                    val n = src.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    copied += n
                    val now = System.currentTimeMillis()
                    if (now - last > 500) {
                        last = now
                        val text = "Copying… " + ModelPicker.sizeText(copied) + if (size > 0) " of " + ModelPicker.sizeText(size) else ""
                        withContext(Dispatchers.Main) { importing = text }
                    }
                }
            }
        }
        target.delete()
        if (!tmp.renameTo(target)) throw IOException("Couldn't save the model")
        val params = ModelPicker.paramsFromName(name).takeIf { it > 0 } ?: (target.length() / 600_000_000.0)
        return InstalledModel("", name, target.path, target.length(), params, "", 0, manual = true)
    }

    // ---- Running the brain -----------------------------------------------------------------------------------
    private val engineLock = Mutex()
    private var process: Process? = null
    private var port = 0
    private var loadedPath = ""
    /** A fresh key each start, so other apps on the phone can't use the brain. */
    var engineKey = ""
        private set
    private val log = ArrayDeque<String>()
    private var idleJob: Job? = null

    val contextSize: Int get() = budget?.context ?: 4096

    /** Starts the brain if needed (loading the model takes a few seconds) and returns its address. */
    suspend fun ensureRunning(): String = withContext(Dispatchers.IO) { engineLock.withLock { startLocked() } }

    private suspend fun startLocked(): String {
        val m = installed ?: throw IOException("There's no on-phone brain yet. Download one in Settings → On-phone brain.")
        process?.let { p ->
            if (p.isAlive && loadedPath == m.path) {
                withContext(Dispatchers.Main) { touch() }
                return "http://127.0.0.1:$port"
            }
        }
        stopEngineLocked()
        val exe = programFile()
        if (!exe.isFile) throw IOException("This build of StellAI doesn't include the on-phone brain program.")
        withContext(Dispatchers.Main) { engineState = "starting"; engineError = "" }
        port = ServerSocket(0).use { it.localPort }
        engineKey = UUID.randomUUID().toString()
        val cores = Runtime.getRuntime().availableProcessors()
        val threads = if (cores >= 8) 4 else maxOf(2, cores / 2)
        val cmd = listOf(exe.path, "-m", m.path, "--host", "127.0.0.1", "--port", port.toString(),
            "-c", contextSize.toString(), "-t", threads.toString(), "-np", "1", "-cram", "0",
            "--reasoning", "off", "--no-webui", "--api-key", engineKey, "-a", "local")
        val pb = ProcessBuilder(cmd).redirectErrorStream(true).directory(app.filesDir)
        pb.environment()["LD_LIBRARY_PATH"] = app.applicationInfo.nativeLibraryDir
        val p = pb.start()
        process = p
        loadedPath = m.path
        synchronized(log) { log.clear() }
        Thread {
            try {
                p.inputStream.bufferedReader().forEachLine { line ->
                    synchronized(log) {
                        log.addLast(line)
                        while (log.size > 60) log.removeFirst()
                    }
                }
            } catch (_: Exception) {
            }
        }.apply { isDaemon = true }.start()
        val base = "http://127.0.0.1:$port"
        val probe = OkHttpClient.Builder().connectTimeout(1, TimeUnit.SECONDS).readTimeout(2, TimeUnit.SECONDS).build()
        val deadline = System.currentTimeMillis() + 180_000
        while (true) {
            if (!p.isAlive) {
                val why = lastLog()
                process = null
                withContext(Dispatchers.Main) { engineState = "failed"; engineError = why }
                throw IOException("The on-phone brain stopped while loading" + if (why.isNotEmpty()) ": $why" else ".")
            }
            val ok = try {
                probe.newCall(Request.Builder().url("$base/health").build()).execute().use { it.code == 200 }
            } catch (_: Exception) {
                false
            }
            if (ok) break
            if (System.currentTimeMillis() > deadline) {
                stopEngineLocked()
                withContext(Dispatchers.Main) { engineState = "failed"; engineError = "Took too long to load." }
                throw IOException("The on-phone brain took too long to load the model.")
            }
            delay(400)
        }
        withContext(Dispatchers.Main) { engineState = "ready"; touch() }
        return base
    }

    /** The last meaningful lines the brain program printed (why it stopped). */
    private fun lastLog(): String = synchronized(log) {
        log.filter { it.contains("error", true) || it.contains("failed", true) || it.contains("unable", true) }
            .takeLast(2).joinToString(" ").ifEmpty { log.lastOrNull().orEmpty() }.take(300)
    }

    /** The brain leaves memory after 10 quiet minutes; the next offline message loads it again. */
    private fun touch() {
        idleJob?.cancel()
        idleJob = app.scope.launch {
            delay(10 * 60_000L)
            if (isActive) stopEngine()
        }
    }

    fun stopEngine() {
        idleJob?.cancel()
        app.scope.launch { engineLock.withLock { stopEngineLocked() } }
    }

    private suspend fun stopEngineLocked() {
        val p = process ?: run { withContext(Dispatchers.Main) { if (engineState != "failed") engineState = "off" }; return }
        process = null
        loadedPath = ""
        withContext(Dispatchers.IO) {
            p.destroy()
            try {
                if (!p.waitFor(3, TimeUnit.SECONDS)) p.destroyForcibly()
            } catch (_: Exception) {
            }
        }
        withContext(Dispatchers.Main) { engineState = "off" }
    }
}
