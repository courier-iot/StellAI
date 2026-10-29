package com.localai.remote

import android.content.Context
import android.speech.tts.TextToSpeech

/** Reads replies aloud with the phone's text-to-speech voice. Main thread only. */
class Speaker(private val context: Context) : TextToSpeech.OnInitListener {

    private var tts: TextToSpeech? = null
    private var ready = false
    private var pending: String? = null

    fun speak(text: String) {
        val clean = text
            .replace(Regex("[*_#`>|]"), "")
            .replace(Regex("\\(done on phone:[^)]*\\)"), "")
            .trim()
        if (clean.isEmpty()) return
        val engine = tts ?: TextToSpeech(context.applicationContext, this).also { tts = it }
        if (ready) {
            engine.speak(clean, TextToSpeech.QUEUE_FLUSH, null, "reply")
        } else {
            pending = clean
        }
    }

    fun stop() {
        pending = null
        if (ready) tts?.stop()
    }

    override fun onInit(status: Int) {
        ready = status == TextToSpeech.SUCCESS
        val text = pending
        pending = null
        if (ready && text != null) tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "reply")
    }
}
