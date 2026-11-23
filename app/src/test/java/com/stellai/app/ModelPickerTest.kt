package com.stellai.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The on-phone brain's choices: model size for the phone, which models and files qualify, and the best pick. */
class ModelPickerTest {
    private val gb = 1_000_000_000L
    private fun phone(ram: Double, free: Double = 30.0, arm64: Boolean = true, sdk: Int = 34) =
        ModelPicker.Device((ram * gb).toLong(), (free * gb).toLong(), arm64, sdk)

    @Test
    fun sizeFollowsThePhonesMemory() {
        assertNull("3 GB phones can't", ModelPicker.budget(phone(2.8)))
        assertEquals(1.2, ModelPicker.budget(phone(3.7))!!.maxParams, 0.0)       // 4 GB phone
        assertEquals(2.0, ModelPicker.budget(phone(5.6))!!.maxParams, 0.0)       // 6 GB phone
        assertEquals(4.5, ModelPicker.budget(phone(7.5))!!.maxParams, 0.0)       // 8 GB phone
        assertEquals("Q5_K_M", ModelPicker.budget(phone(11.3))!!.quants.first()) // 12 GB phone
        assertNull("needs 64-bit ARM", ModelPicker.budget(phone(8.0, arm64 = false)))
        assertNull("needs Android 9", ModelPicker.budget(phone(8.0, sdk = 27)))
        assertNull("needs storage", ModelPicker.budget(phone(8.0, free = 1.1)))
        assertEquals("limited by free storage", 1_500_000_000L, ModelPicker.budget(phone(7.5, free = 2.5))!!.maxFile)
        assertTrue(ModelPicker.whyNot(phone(8.0, sdk = 27)).contains("Android 9"))
    }

    @Test
    fun parametersFromNames() {
        assertEquals(1.7, ModelPicker.paramsFromName("unsloth/Qwen3-1.7B-GGUF"), 0.0)
        assertEquals(3.0, ModelPicker.paramsFromName("bartowski/meta-llama_Llama-3.2-3B-Instruct-GGUF"), 0.0)
        assertEquals(30.0, ModelPicker.paramsFromName("unsloth/Qwen3-30B-A3B-GGUF"), 0.0)
        assertEquals(1.0, ModelPicker.paramsFromName("unsloth/gemma-3-1b-it-GGUF"), 0.0)
        assertEquals(0.0, ModelPicker.paramsFromName("bartowski/Phi-3.5-mini-instruct-GGUF"), 0.0)
        assertEquals(0.6, ModelPicker.paramsFromName("Qwen/Qwen3-0.6B-GGUF"), 0.0)
    }

    @Test
    fun onlyChatModelsThatFit() {
        val b = ModelPicker.budget(phone(5.6))!!   // up to 2B
        fun c(repo: String, dl: Long = 50_000, gated: Boolean = false) =
            ModelPicker.Candidate(repo, ModelPicker.paramsFromName(repo), dl, 10, "2025-05-01T00:00:00.000Z", gated)
        val kept = ModelPicker.filter(listOf(
            c("unsloth/Qwen3-1.7B-GGUF"),
            c("unsloth/Qwen3-4B-GGUF"),                    // too big
            c("Qwen/Qwen3-Embedding-0.6B-GGUF"),           // not a chat model
            c("unsloth/Qwen2.5-VL-3B-Instruct-GGUF"),      // vision, and too big
            c("unsloth/Qwen2.5-Coder-1.5B-Instruct-GGUF"), // coder
            c("someone/Qwen3-1.7B-Base-GGUF"),             // base model
            c("bartowski/Phi-3.5-mini-instruct-GGUF"),     // size unknown: checked by file later
            c("unsloth/gemma-3-1b-it-GGUF", gated = true), // needs an account
            c("bartowski/obscure-1B-GGUF", dl = 12),       // nobody uses it
            c("Unsloth/qwen3-1.7b-gguf"),                  // the same as the first
        ), b).map { it.repo }
        assertEquals(listOf("unsloth/Qwen3-1.7B-GGUF", "bartowski/Phi-3.5-mini-instruct-GGUF"), kept)
    }

    @Test
    fun bestFileThatFits() {
        val b = ModelPicker.budget(phone(7.5))!!
        val files = listOf(
            ModelPicker.RepoFile("Qwen3-4B-BF16.gguf", 8_000_000_000, "a"),
            ModelPicker.RepoFile("Qwen3-4B-Q8_0.gguf", 4_300_000_000, "b"),
            ModelPicker.RepoFile("Qwen3-4B-Q5_K_M.gguf", 2_900_000_000, "c"),
            ModelPicker.RepoFile("Qwen3-4B-Q4_K_M.gguf", 2_500_000_000, "d"),
            ModelPicker.RepoFile("Qwen3-4B-Q4_0.gguf", 2_370_000_000, "e"),
            ModelPicker.RepoFile("mmproj-F16.gguf", 600_000_000, "f"),
            ModelPicker.RepoFile("README.md", 5_000, ""),
        )
        assertEquals("Qwen3-4B-Q4_K_M.gguf", ModelPicker.pickFile(files, b)!!.path)
        val small = ModelPicker.budget(phone(3.7))!!
        assertNull("nothing under 850 MB", ModelPicker.pickFile(files, small))
        val split = listOf(ModelPicker.RepoFile("big-Q4_K_M-00001-of-00002.gguf", 1_000_000_000, "x"))
        assertNull("split models aren't used", ModelPicker.pickFile(split, b))
        assertEquals("Q4_K_M", ModelPicker.quantOf("Qwen_Qwen3-4B-Q4_K_M.gguf"))
        assertEquals("IQ4_XS", ModelPicker.quantOf("model-IQ4_XS.gguf"))
        assertEquals("Q8_0", ModelPicker.quantOf("Llama-3.2-3B-Instruct-Q8_0.gguf"))
    }

    @Test
    fun biggerAndNewerWins() {
        val b = ModelPicker.budget(phone(7.5))!!
        val today = ModelPicker.daysOf("2026-11-23")!!
        val newSmall = ModelPicker.score(1.7, "2026-05-01", 100_000, b, today)
        val newBig = ModelPicker.score(4.0, "2026-05-01", 100_000, b, today)
        val oldBig = ModelPicker.score(4.0, "2023-01-01", 1_000_000, b, today)
        assertTrue(newBig > newSmall)
        assertTrue(newBig > oldBig)
        assertEquals(null, ModelPicker.daysOf("not a date"))
    }

    @Test
    fun upgradesOnlyWhenClearlyBetter() {
        val c = ModelPicker.Candidate("unsloth/Qwen4-4B-GGUF", 4.0, 1, 1, "")
        val best = ModelPicker.Choice(c, ModelPicker.RepoFile("x-Q4_K_M.gguf", 1, ""), 4.0, 2.0)
        assertTrue(ModelPicker.isUpgrade(best, "unsloth/Qwen3-1.7B-GGUF", 1.5, emptySet()))
        assertFalse("same model", ModelPicker.isUpgrade(best, "Unsloth/Qwen4-4B-GGUF", 1.0, emptySet()))
        assertFalse("barely better", ModelPicker.isUpgrade(best, "a/b", 1.95, emptySet()))
        assertFalse("turned down before", ModelPicker.isUpgrade(best, "a/b", 1.0, setOf("unsloth/qwen4-4b-gguf")))
    }

    @Test
    fun namesAndSizesForPeople() {
        assertEquals("Qwen3 1.7B", ModelPicker.displayName("unsloth/Qwen3-1.7B-GGUF"))
        assertEquals("Qwen3 4B", ModelPicker.displayName("bartowski/Qwen_Qwen3-4B-GGUF"))
        assertEquals("Llama 3.2 3B Instruct", ModelPicker.displayName("bartowski/meta-llama_Llama-3.2-3B-Instruct-GGUF"))
        assertEquals("1.1 GB", ModelPicker.sizeText(1_100_000_000))
        assertEquals("850 MB", ModelPicker.sizeText(850_000_000))
        assertNotNull(ModelPicker.TRUSTED_AUTHORS.firstOrNull { it == "Qwen" })
    }
}
