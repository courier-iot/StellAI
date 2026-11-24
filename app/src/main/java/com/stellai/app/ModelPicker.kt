package com.stellai.app

import kotlin.math.ln
import kotlin.math.max

/**
 * The on-phone brain's choices, with no Android or network code (so the tests can check them):
 * how big a model this phone can run, which Hugging Face models qualify, which file of a model to download,
 * and which model is best.
 */
object ModelPicker {

    /** What matters about the phone. Sizes in bytes. */
    data class Device(val totalRam: Long, val freeStorage: Long, val arm64: Boolean, val sdk: Int)

    /**
     * How big a model this phone gets. [maxParams] in billions; [quants] best first.
     * [context]: how much of the chat the model holds at once (tokens).
     */
    data class Budget(val label: String, val maxFile: Long, val maxParams: Double, val context: Int, val quants: List<String>)

    /** A model found on Hugging Face (one repository). [params] in billions, 0 when the name doesn't say. */
    data class Candidate(
        val repo: String,
        val params: Double,
        val downloads: Long,
        val likes: Long,
        val createdAt: String,
        val gated: Boolean = false,
    )

    /** One file in a repository. */
    data class RepoFile(val path: String, val size: Long, val sha256: String)

    /** A model ready to download: the repository and the file picked from it. */
    data class Choice(val candidate: Candidate, val file: RepoFile, val params: Double, val score: Double)

    /** Uploaders whose models are searched by default: the model makers themselves and well-known converters. */
    val TRUSTED_AUTHORS = listOf(
        "Qwen", "bartowski", "unsloth", "lmstudio-community", "ggml-org", "microsoft", "ibm-granite", "HuggingFaceTB",
    )

    /** Android needs 9 (API 28) for the brain program, and a 64-bit ARM processor. */
    const val MIN_SDK = 28

    private const val GB = 1_000_000_000L

    private val SMALL_QUANTS = listOf("Q4_K_M", "Q4_K_S", "IQ4_XS", "Q4_0", "Q3_K_L", "Q5_K_M")
    private val BIG_QUANTS = listOf("Q5_K_M", "Q4_K_M", "Q6_K", "Q4_K_S", "IQ4_XS", "Q4_0")

    /**
     * The size of model this phone can run, by its memory (phones report a little less than the box says:
     * an 8 GB phone shows about 7.5 GB). [stepDown]: go this many sizes smaller (after Android stopped a model for
     * using too much memory). Null: the phone can't run one.
     */
    fun budget(d: Device, stepDown: Int = 0): Budget? {
        if (!d.arm64 || d.sdk < MIN_SDK) return null
        val ram = d.totalRam
        val tiers = listOf(
            Budget("Small (4 GB phone)", 850_000_000L, 1.2, 4096, SMALL_QUANTS),
            Budget("Medium (6 GB phone)", 1_400_000_000L, 2.0, 4096, SMALL_QUANTS),
            Budget("Large (8 GB phone)", 2_700_000_000L, 4.5, 8192, SMALL_QUANTS),
            Budget("Extra large (12 GB+ phone)", 3_600_000_000L, 4.5, 8192, BIG_QUANTS),
        )
        val tier = when {
            ram < 3_300_000_000L -> return null
            ram < 5 * GB -> 0
            ram < 7 * GB -> 1
            ram < 10 * GB -> 2
            else -> 3
        }
        val b = tiers[(tier - stepDown.coerceAtLeast(0)).coerceAtLeast(0)]
        // keep 1 GB of storage free for everything else on the phone
        val room = d.freeStorage - GB
        return if (room < 300_000_000L) null else b.copy(maxFile = minOf(b.maxFile, room))
    }

    /** Why [budget] said no, in words. */
    fun whyNot(d: Device): String = when {
        !d.arm64 -> "This phone's processor isn't 64-bit ARM, which the on-phone brain needs."
        d.sdk < MIN_SDK -> "The on-phone brain needs Android 9 or newer."
        d.totalRam < 3_300_000_000L -> "This phone has too little memory (under 4 GB) for an on-phone brain."
        else -> "There isn't enough free storage. Free up at least 1.5 GB and try again."
    }

    private val PARAMS = Regex("(?i)(?<![\\d.])(\\d{1,3}(?:\\.\\d{1,2})?)\\s*b(?![a-z])")
    private val EXCLUDE = Regex(
        "(?i)(embed|rerank|(?<![a-z])vl(?![a-z])|vision|(?<![a-z])base(?![a-z])|guard|math|coder|draft|abliterat|uncensor|" +
            "reward|prm(?![a-z])|tts|audio|omni|ocr|nsfw|roleplay|(?<![a-z])rp(?![a-z])|-moe-|test|tiny-random|lora|" +
            "(?<![a-z])mtp(?![a-z]))"
    )

    /** Parameter count (billions) from a model's name, e.g. "Qwen3-1.7B" -> 1.7, "Qwen3-30B-A3B" -> 30. 0 if unknown. */
    fun paramsFromName(name: String): Double =
        PARAMS.find(name.substringAfter('/'))?.groupValues?.get(1)?.toDoubleOrNull() ?: 0.0

    /** Search results worth looking at: chat models, not gated, small enough by their name. */
    fun filter(found: List<Candidate>, b: Budget): List<Candidate> = found.filter { c ->
        !c.gated && !EXCLUDE.containsMatchIn(c.repo.substringAfter('/')) &&
            (c.params == 0.0 || c.params <= b.maxParams) && c.downloads >= 500
    }.distinctBy { it.repo.lowercase() }

    /**
     * The file to download from a repository: the best quantization in [b]'s order that fits,
     * skipping vision add-ons (mmproj) and models split into parts. Null if none fits.
     */
    fun pickFile(files: List<RepoFile>, b: Budget): RepoFile? {
        val usable = files.filter { f ->
            val n = f.path.substringAfterLast('/').lowercase()
            // mtp-…: a version with extra layers for a speed trick, bigger and needing more memory
            n.endsWith(".gguf") && "mmproj" !in n && !Regex("(?<![a-z])mtp(?![a-z])").containsMatchIn(n) && !Regex("-\\d{5}-of-\\d{5}").containsMatchIn(n) && f.size in 1..b.maxFile
        }
        for (q in b.quants) {
            val hit = usable.filter { quantOf(it.path).equals(q, ignoreCase = true) }.minByOrNull { it.size }
            if (hit != null) return hit
        }
        return null
    }

    /** "Qwen3-4B-Q4_K_M.gguf" -> "Q4_K_M" ("" if the name doesn't say). */
    fun quantOf(path: String): String =
        Regex("(?i)(?<![a-z0-9])(I?Q\\d(?:_[A-Z0-9]+)*)(?=\\.gguf$)").find(path.substringAfterLast('/'))?.groupValues?.get(1)?.uppercase()
            ?: Regex("(?i)[-_.](I?Q\\d(?:_[A-Z0-9]+)*)[-_.]").find(path.substringAfterLast('/'))?.groupValues?.get(1)?.uppercase()
            ?: ""

    /** Billions of parameters, from the name or else roughly from a 4-bit file's size. */
    fun paramsOf(c: Candidate, file: RepoFile): Double =
        if (c.params > 0) c.params else file.size / 600_000_000.0

    /**
     * How good a pick this is (higher is better): bigger is smarter (up to what the phone runs), newer models beat
     * older ones of the same size, and popularity breaks ties. [today] as days since 1970.
     */
    fun score(params: Double, createdAt: String, downloads: Long, b: Budget, today: Long): Double {
        val size = (params / b.maxParams).coerceIn(0.0, 1.0)
        val age = daysOf(createdAt)?.let { today - it } ?: 730
        val recency = (1.0 - age / 730.0).coerceIn(0.0, 1.0)
        val popular = (ln(max(1L, downloads).toDouble()) / ln(10.0) / 7.0).coerceIn(0.0, 1.0)
        return size * 1.0 + recency * 0.9 + popular * 0.3
    }

    /** Days since 1970 from "2025-04-29T…" (null if it can't be read). */
    fun daysOf(iso: String): Long? = try {
        java.time.LocalDate.parse(iso.take(10)).toEpochDay()
    } catch (_: Exception) {
        null
    }

    /** True when [best] is worth suggesting over the model already on the phone. */
    fun isUpgrade(best: Choice, installedRepo: String, installedScore: Double, dismissed: Set<String>): Boolean =
        !best.candidate.repo.equals(installedRepo, ignoreCase = true) &&
            best.candidate.repo.lowercase() !in dismissed.map { it.lowercase() } &&
            best.score > installedScore + 0.1

    /** "1.1 GB", "850 MB". */
    fun sizeText(bytes: Long): String =
        if (bytes >= GB) String.format(java.util.Locale.US, "%.1f GB", bytes / GB.toDouble())
        else "${bytes / 1_000_000} MB"

    /** "Qwen3 1.7B" from "unsloth/Qwen3-1.7B-GGUF". */
    fun displayName(repo: String): String =
        repo.substringAfter('/').replace(Regex("(?i)[-_.]?gguf$"), "").replace(Regex("^(Qwen|meta-llama|microsoft|ibm-granite|HuggingFaceTB)_"), "")
            .replace('_', ' ').replace('-', ' ')
}
