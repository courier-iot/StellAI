package com.stellai.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener

/** Reads replies aloud with the phone's text-to-speech voice. Main thread only. */
class Speaker(private val context: Context) : TextToSpeech.OnInitListener {

    private var tts: TextToSpeech? = null
    private var ready = false
    private val pending = mutableListOf<Pair<String, Boolean>>()   // text, flush first
    private var counter = 0
    private var lastId = ""
    private var speaking = false
    private var doneCallback: (() -> Unit)? = null
    private val main = Handler(Looper.getMainLooper())

    /** Says this now, replacing anything being said. */
    fun speak(text: String) = say(text, flush = true)

    /** Says this after whatever is being said (for a reply that arrives a phrase at a time). */
    fun enqueue(text: String) = say(text, flush = false)

    /** Runs once everything queued has been said (right away if nothing is being said). */
    fun whenDone(block: () -> Unit) {
        if (!speaking && pending.isEmpty()) block() else doneCallback = block
    }

    private fun say(text: String, flush: Boolean) {
        val clean = SpeechText.forSpeech(text)
        if (clean.isEmpty()) return
        val engine = tts ?: TextToSpeech(context.applicationContext, this).also { tts = it }
        if (ready) {
            val id = "s${counter++}"
            lastId = id
            speaking = true
            engine.speak(clean, if (flush) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD, null, id)
        } else {
            if (flush) pending.clear()
            pending.add(clean to flush)
        }
    }

    fun stop() {
        pending.clear()
        doneCallback = null
        speaking = false
        if (ready) tts?.stop()
    }

    override fun onInit(status: Int) {
        ready = status == TextToSpeech.SUCCESS
        tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {}
            override fun onDone(utteranceId: String?) = finished(utteranceId)
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) = finished(utteranceId)
            override fun onError(utteranceId: String?, errorCode: Int) = finished(utteranceId)
        })
        val queued = pending.toList()
        pending.clear()
        if (ready) queued.forEach { (text, flush) -> say(text, flush) }
    }

    private fun finished(id: String?) {
        main.post {
            if (id != lastId) return@post
            speaking = false
            val cb = doneCallback
            doneCallback = null
            cb?.invoke()
        }
    }
}

/**
 * Feeds a reply that's still streaming in to the voice: the first natural phrase (up to a comma) as soon as it's
 * there, then whole sentences. Code blocks are skipped. Call [feed] with the full text so far.
 */
class SentenceFeeder(private val emit: (String) -> Unit) {
    private var fed = 0
    private var firstDone = false
    var saidAny = false

    fun begin() {
        fed = 0
    }

    fun feed(text: String, finish: Boolean = false) {
        if (fed > text.length) fed = 0
        val rest = text.substring(fed)
        if (!finish && Regex("```").findAll(text.substring(0, fed)).count() % 2 == 1) return
        var cut = -1
        if (finish) {
            cut = rest.length
        } else {
            val rx = if (firstDone) Regex("[.!?…:;][\"')\\]]?\\s+|\\n{2,}") else Regex("[.!?…:;,—][\"')\\]]?\\s+|\\n{2,}")
            val min = if (firstDone) 40 else 24
            for (m in rx.findAll(rest)) {
                if (m.range.last + 1 >= min || m.value.contains('\n')) {
                    cut = m.range.last + 1
                    if (!firstDone) break
                }
            }
            val fence = rest.indexOf("```")
            if (fence >= 0 && (cut < 0 || fence < cut)) {
                val close = rest.indexOf("```", fence + 3)
                cut = if (close < 0) (if (fence > 0) fence else -1) else close + 3
            }
        }
        if (cut <= 0) return
        val piece = rest.substring(0, cut)
        fed += cut
        if (piece.isNotBlank()) {
            firstDone = true
            saidAny = true
            emit(piece)
        }
    }
}
