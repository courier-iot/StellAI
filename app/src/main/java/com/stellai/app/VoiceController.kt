package com.stellai.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.sqrt

enum class VoicePhase { IDLE, WAITING_FOR_WAKE, LISTENING, TRANSCRIBING }

/**
 * Owns the microphone. One background thread reads audio and either watches for
 * the wake word (when the assistant service is on) or records a single command
 * (mic button / assist gesture). Commands go to Whisper on the laptop, then to the chat.
 */
class VoiceController(private val app: App) {

    var phase by mutableStateOf(VoicePhase.IDLE)
        private set
    var lastError by mutableStateOf<String?>(null)
        private set

    private val main = Handler(Looper.getMainLooper())
    @Volatile
    private var wakeMode = false
    private val listenRequested = AtomicBoolean(false)
    @Volatile
    private var cancelCapture = false
    private var thread: Thread? = null
    private val lock = Any()
    private var noiseFloor = 300.0

    fun hasMicPermission() =
        ContextCompat.checkSelfPermission(app, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    /** Turned on and off by AssistantService. */
    fun setWakeMode(on: Boolean) {
        wakeMode = on
        if (on) ensureRunning()
    }

    /** Record one command now (mic button, power-button assist). */
    fun listenNow() {
        if (!hasMicPermission()) {
            lastError = "Microphone permission is needed"
            return
        }
        cancelCapture = false
        listenRequested.set(true)
        ensureRunning()
    }

    fun clearError() {
        lastError = null
    }

    fun cancel() {
        listenRequested.set(false)
        cancelCapture = true
    }

    private fun ensureRunning() {
        synchronized(lock) {
            if (thread?.isAlive == true) return
            thread = Thread({ runLoop() }, "voice").apply { start() }
        }
    }

    @Volatile
    private var postedPhase = VoicePhase.IDLE

    private fun updatePhase(p: VoicePhase) {
        if (p == postedPhase) return
        postedPhase = p
        main.post { phase = p }
    }

    private fun postError(msg: String?) {
        main.post { lastError = msg }
    }

    @SuppressLint("MissingPermission") // checked in hasMicPermission()
    private fun runLoop() {
        var recorder: AudioRecord? = null
        var detector: WakeWordDetector? = null
        var detectorModel = ""
        var failed = false
        try {
            if (!hasMicPermission()) {
                postError("Microphone permission is needed")
                failed = true
                return
            }
            val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            recorder = AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, RATE * 2), // 1 s of buffer
            )
            if (recorder.state != AudioRecord.STATE_INITIALIZED) {
                postError("Couldn't open the microphone")
                failed = true
                return
            }
            recorder.startRecording()
            postError(null)

            val chunk = ShortArray(WakeWordDetector.CHUNK_SAMPLES)
            val preRoll = ShortRing(RATE / 2) // keep the last 0.5 s for after the wake word
            var cooldownUntil = 0L

            while (true) {
                val wake = wakeMode
                if (!wake && !listenRequested.get()) break
                updatePhase(if (wake) VoicePhase.WAITING_FOR_WAKE else VoicePhase.IDLE)

                if (!readFully(recorder, chunk)) {
                    postError("Microphone stopped working")
                    failed = true
                    break
                }
                preRoll.add(chunk)
                trackNoise(chunk)

                if (listenRequested.getAndSet(false)) {
                    handleCommand(recorder, ShortArray(0))
                    detector?.reset()
                    continue
                }

                if (wake) {
                    val model = app.settings.wakeWord
                    if (detector == null || detectorModel != model) {
                        detector?.close()
                        detector = WakeWordDetector(app, model)
                        detectorModel = model
                    }
                    val score = detector.process(chunk)
                    val now = System.currentTimeMillis()
                    if (score >= app.settings.wakeThreshold && now > cooldownUntil && !app.engine.busy) {
                        Log.i(TAG, "wake word detected (score $score)")
                        handleCommand(recorder, preRoll.snapshot())
                        detector.reset()
                        cooldownUntil = System.currentTimeMillis() + 1500
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "voice loop failed", e)
            postError(e.message ?: "Voice error")
            failed = true
        } finally {
            try {
                recorder?.stop()
            } catch (_: Exception) {
            }
            recorder?.release()
            detector?.close()
            updatePhase(VoicePhase.IDLE)
            synchronized(lock) { thread = null }
            // A request may have arrived while we were shutting down (but don't spin on errors).
            if (!failed && (wakeMode || listenRequested.get())) ensureRunning()
        }
    }

    /** Records until the user stops talking, transcribes on the laptop, sends to the chat. */
    private fun handleCommand(recorder: AudioRecord, preRoll: ShortArray) {
        cancelCapture = false
        main.post { app.speaker.stop() }
        beep(ToneGenerator.TONE_PROP_BEEP)
        updatePhase(VoicePhase.LISTENING)

        val pcm = recordUtterance(recorder, preRoll)
        if (pcm == null) {
            if (!cancelCapture) beep(ToneGenerator.TONE_PROP_NACK)
            return
        }
        beep(ToneGenerator.TONE_PROP_ACK)
        updatePhase(VoicePhase.TRANSCRIBING)

        val text = try {
            HubApi.transcribe(app.settings.serverUrl, app.settings.apiKey, pcm)
        } catch (e: Exception) {
            Log.w(TAG, "transcribe failed", e)
            postError("Speech recognition failed: ${e.message}")
            main.post { if (app.settings.speakReplies) app.speaker.speak("I can't reach the server.") }
            return
        }
        val command = stripWakeWord(text)
        if (command.isBlank()) {
            beep(ToneGenerator.TONE_PROP_NACK)
            return
        }
        main.post { app.engine.send(command, fromVoice = true) }
    }

    /**
     * Simple voice activity detection: wait up to 6 s for speech, then stop
     * after 0.9 s of quiet (or 15 s total).
     */
    private fun recordUtterance(recorder: AudioRecord, preRoll: ShortArray): ShortArray? {
        val out = ShortBuilder(RATE * 4)
        out.add(preRoll, preRoll.size)
        val frame = ShortArray(RATE / 50) // 20 ms
        val threshold = maxOf(500.0, noiseFloor * 3.0)
        var speechMs = 0
        var silenceMs = 0
        var elapsed = 0
        var started = false
        while (elapsed < 15_000) {
            if (cancelCapture) return null
            if (!readFully(recorder, frame)) return null
            out.add(frame, frame.size)
            elapsed += 20
            if (rms(frame, frame.size) > threshold) {
                speechMs += 20
                silenceMs = 0
                if (speechMs >= 120) started = true
            } else {
                silenceMs += 20
            }
            if (!started && elapsed >= 6_000) return null
            if (started && silenceMs >= 900) break
        }
        return if (started) out.toArray() else null
    }

    private fun trackNoise(chunk: ShortArray) {
        val level = rms(chunk, chunk.size)
        // Follow quiet levels quickly and loud ones slowly, so speech doesn't count as noise.
        noiseFloor = if (level < noiseFloor) 0.9 * noiseFloor + 0.1 * level
        else 0.995 * noiseFloor + 0.005 * minOf(level, noiseFloor * 3)
    }

    private fun readFully(recorder: AudioRecord, buf: ShortArray): Boolean {
        var off = 0
        while (off < buf.size) {
            val n = recorder.read(buf, off, buf.size - off)
            if (n <= 0) return false
            off += n
        }
        return true
    }

    private fun beep(tone: Int) {
        try {
            val gen = ToneGenerator(AudioManager.STREAM_MUSIC, 70)
            gen.startTone(tone, 150)
            main.postDelayed({ gen.release() }, 400)
        } catch (_: RuntimeException) {
        }
    }

    private class ShortRing(capacity: Int) {
        private val buf = ShortArray(capacity)
        private var pos = 0
        private var filled = 0
        fun add(data: ShortArray) {
            for (s in data) {
                buf[pos] = s
                pos = (pos + 1) % buf.size
            }
            filled = minOf(buf.size, filled + data.size)
        }

        fun snapshot(): ShortArray {
            val out = ShortArray(filled)
            val start = (pos - filled + buf.size) % buf.size
            for (i in 0 until filled) out[i] = buf[(start + i) % buf.size]
            return out
        }
    }

    private class ShortBuilder(initial: Int) {
        private var buf = ShortArray(initial)
        private var size = 0
        fun add(data: ShortArray, n: Int) {
            if (size + n > buf.size) buf = buf.copyOf(maxOf(buf.size * 2, size + n))
            System.arraycopy(data, 0, buf, size, n)
            size += n
        }

        fun toArray(): ShortArray = buf.copyOf(size)
    }

    companion object {
        private const val TAG = "VoiceController"
        const val RATE = WakeWordDetector.SAMPLE_RATE

        private val WAKE_PREFIX = Regex(
            "^\\W*(?:(?:hey|hi|ok|okay|a)\\W+)?(?:j?arvis|a?lexa|(?:my)?croft)\\b\\W*",
            RegexOption.IGNORE_CASE,
        )

        /** The recording starts slightly before the wake word ends; drop it from the text. */
        fun stripWakeWord(text: String): String = text.replace(WAKE_PREFIX, "").trim()

        fun rms(data: ShortArray, n: Int): Double {
            if (n == 0) return 0.0
            var sum = 0.0
            for (i in 0 until n) {
                val v = data[i].toDouble()
                sum += v * v
            }
            return sqrt(sum / n)
        }
    }
}
