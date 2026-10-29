package com.stellai.app

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import ai.onnxruntime.TensorInfo
import android.content.Context
import java.io.Closeable
import java.nio.FloatBuffer

/**
 * openWakeWord, running on the phone with ONNX Runtime.
 *
 * Same pipeline as the Python library (openwakeword/utils.py + model.py):
 *  1. every 80 ms (1280 samples @ 16 kHz) compute mel frames from the newest audio
 *     plus 480 samples of context; frames are transformed with x/10 + 2
 *  2. each step, the last 76 mel frames -> embedding model -> one 96-value feature
 *  3. the last 16 features -> wake word model -> score 0..1
 *
 * The shared models live in assets/wakeword (downloaded by the GitHub build workflow);
 * the wake word model itself is built in, or a custom one trained by you and kept on the server.
 */
class WakeWordDetector(context: Context, wakeModel: ByteArray) : Closeable {

    private val env = OrtEnvironment.getEnvironment()
    private val opts = OrtSession.SessionOptions().apply {
        setIntraOpNumThreads(1) // plenty for 80 ms chunks, and easier on the battery
    }
    private val melSession = load(context, "wakeword/melspectrogram.onnx")
    private val embSession = load(context, "wakeword/embedding_model.onnx")
    private val wakeSession = env.createSession(wakeModel, opts)

    private val melInput = melSession.inputNames.first()
    private val embInput = embSession.inputNames.first()
    private val wakeInput = wakeSession.inputNames.first()
    private val featureFrames: Int = try {
        val info = wakeSession.inputInfo.values.first().info as TensorInfo
        info.shape[1].toInt().takeIf { it > 0 } ?: 16
    } catch (e: Exception) {
        16
    }

    private val tail = FloatArray(CONTEXT_SAMPLES) // last 480 samples, used as context
    private val melFrames = ArrayDeque<FloatArray>()
    private val features = ArrayDeque<FloatArray>()
    private var predictions = 0

    init {
        reset()
    }

    /** Feed exactly 1280 samples (80 ms). Returns the wake word score, 0..1. */
    fun process(chunk: ShortArray): Float {
        require(chunk.size == CHUNK_SAMPLES) { "need $CHUNK_SAMPLES samples" }

        // 1. Mel spectrogram of context + new audio (raw int16 values as floats, like the Python code)
        val audio = FloatArray(CONTEXT_SAMPLES + CHUNK_SAMPLES)
        System.arraycopy(tail, 0, audio, 0, CONTEXT_SAMPLES)
        for (i in chunk.indices) audio[CONTEXT_SAMPLES + i] = chunk[i].toFloat()
        System.arraycopy(audio, audio.size - CONTEXT_SAMPLES, tail, 0, CONTEXT_SAMPLES)

        val mel = run(melSession, melInput, audio, longArrayOf(1, audio.size.toLong()))
        val frames = mel.size / MEL_BINS
        for (f in 0 until frames) {
            melFrames.addLast(FloatArray(MEL_BINS) { b -> mel[f * MEL_BINS + b] / 10f + 2f })
        }
        while (melFrames.size > MAX_MEL_FRAMES) melFrames.removeFirst()

        // 2. Embedding of the last 76 mel frames
        if (melFrames.size >= EMBED_WINDOW) {
            val window = FloatArray(EMBED_WINDOW * MEL_BINS)
            val start = melFrames.size - EMBED_WINDOW
            for (i in 0 until EMBED_WINDOW) System.arraycopy(melFrames[start + i], 0, window, i * MEL_BINS, MEL_BINS)
            val emb = run(embSession, embInput, window, longArrayOf(1, EMBED_WINDOW.toLong(), MEL_BINS.toLong(), 1))
            features.addLast(emb)
            while (features.size > featureFrames * 2) features.removeFirst()
        }

        // 3. Wake word score from the last 16 features
        if (features.size < featureFrames) return 0f
        val dim = features.last().size
        val input = FloatArray(featureFrames * dim)
        val start = features.size - featureFrames
        for (i in 0 until featureFrames) System.arraycopy(features[start + i], 0, input, i * dim, dim)
        val score = run(wakeSession, wakeInput, input, longArrayOf(1, featureFrames.toLong(), dim.toLong()))
            .firstOrNull() ?: 0f

        // The Python library ignores the first 5 predictions while buffers settle.
        predictions++
        return if (predictions <= 5) 0f else score
    }

    /** Call after a detection so the same words don't trigger twice. */
    fun reset() {
        melFrames.clear()
        repeat(EMBED_WINDOW) { melFrames.addLast(FloatArray(MEL_BINS) { 1f }) } // like np.ones((76, 32))
        features.clear()
        predictions = 0
    }

    private fun run(session: OrtSession, inputName: String, data: FloatArray, shape: LongArray): FloatArray {
        OnnxTensor.createTensor(env, FloatBuffer.wrap(data), shape).use { tensor ->
            session.run(mapOf(inputName to tensor)).use { result ->
                val out = (result.get(0) as OnnxTensor).floatBuffer
                return FloatArray(out.remaining()).also { out.get(it) }
            }
        }
    }

    private fun load(context: Context, asset: String): OrtSession {
        val bytes = context.assets.open(asset).use { it.readBytes() }
        return env.createSession(bytes, opts)
    }

    override fun close() {
        melSession.close()
        embSession.close()
        wakeSession.close()
        opts.close()
    }

    companion object {
        const val SAMPLE_RATE = 16000
        const val CHUNK_SAMPLES = 1280
        private const val CONTEXT_SAMPLES = 160 * 3
        private const val MEL_BINS = 32
        private const val EMBED_WINDOW = 76
        private const val MAX_MEL_FRAMES = 200
    }
}
