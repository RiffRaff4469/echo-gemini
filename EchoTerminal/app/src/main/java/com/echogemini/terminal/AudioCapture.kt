package com.echogemini.terminal

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Microphone capture: 16 kHz mono PCM16, ~20 ms frames, software gain, VAD gate.
 *
 * **Software gain is not optional here.** On LineageOS only one mic of the
 * array is live and its gain is low (HANDOFF section 2) -- this is called out as
 * the single largest technical risk in the project. The multiplier is pushed
 * down by the server from `MIC_GAIN`, whose real value comes from the Phase 3
 * measurement recorded in docs/HARDWARE-STATUS.md. Until that measurement
 * exists, 1.0 is a placeholder, not a calibration.
 *
 * The VAD gate means silence is never streamed, so an idle device costs the
 * link roughly nothing (HANDOFF section 7). The device makes no judgement about
 * meaning -- it decides only "is this loud enough to be worth sending".
 */
class AudioCapture(private val sink: (ByteArray) -> Unit) {

    /**
     * Multiplier applied before the gate. Set from the server's `mic` message.
     * Clamped because too much gain turns a quiet room into constant "speech"
     * and holds a billed Live session open.
     */
    @Volatile
    var gain: Float = 1.0f
        set(value) {
            val clamped = value.coerceIn(0.1f, 32f)
            if (abs(clamped - field) > 0.001f) {
                Log.i(TAG, "software gain -> $clamped")
            }
            field = clamped
        }

    /**
     * Uplink gate. The server closes this while it is speaking: half-duplex
     * deliberately sidesteps acoustic echo cancellation on a device with one
     * weak mic (HANDOFF section 8.2).
     */
    @Volatile
    var uplinkEnabled: Boolean = true

    /** Most recent frame RMS after gain -- surfaced for on-device level checks. */
    @Volatile
    var lastRms: Float = 0f
        private set

    @Volatile
    private var running = false

    private var record: AudioRecord? = null
    private var thread: Thread? = null

    private var speechFramesSent = 0L
    private var hangoverFramesLeft = 0

    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (running) return true

        val minBuffer = AudioRecord.getMinBufferSize(
            Protocol.AUDIO_UP_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuffer <= 0) {
            Log.e(TAG, "AudioRecord.getMinBufferSize returned $minBuffer; no usable mic")
            return false
        }

        // Several frames of headroom so a GC pause does not drop audio.
        val bufferBytes = maxOf(minBuffer, Protocol.AUDIO_UP_FRAME_BYTES * 10)

        val recorder = try {
            AudioRecord(
                // VOICE_RECOGNITION rather than MIC: it asks the platform not to
                // apply automatic gain control or noise suppression, which on
                // this hardware do more harm than good and would fight the
                // software gain above.
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                Protocol.AUDIO_UP_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferBytes
            )
        } catch (e: Exception) {
            Log.e(TAG, "could not construct AudioRecord", e)
            return false
        }

        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord failed to initialise (state=${recorder.state})")
            recorder.release()
            return false
        }

        record = recorder
        running = true
        recorder.startRecording()
        thread = Thread(::loop, "echo-audio-capture").apply {
            priority = Thread.MAX_PRIORITY - 1
            start()
        }
        Log.i(TAG, "capture started: ${Protocol.AUDIO_UP_RATE} Hz mono PCM16, ${Protocol.AUDIO_FRAME_MS} ms frames")
        return true
    }

    fun stop() {
        running = false
        thread?.join(500)
        thread = null
        record?.let {
            try {
                if (it.recordingState == AudioRecord.RECORDSTATE_RECORDING) it.stop()
            } catch (e: IllegalStateException) {
                Log.w(TAG, "AudioRecord.stop threw", e)
            }
            it.release()
        }
        record = null
        Log.i(TAG, "capture stopped after $speechFramesSent gated frames")
    }

    private fun loop() {
        val recorder = record ?: return
        val samples = ShortArray(Protocol.AUDIO_UP_FRAME_SAMPLES)
        val outBytes = ByteArray(Protocol.AUDIO_UP_FRAME_BYTES)

        while (running) {
            val read = recorder.read(samples, 0, samples.size)
            if (read <= 0) {
                if (read == AudioRecord.ERROR_INVALID_OPERATION || read == AudioRecord.ERROR_BAD_VALUE) {
                    Log.e(TAG, "AudioRecord.read error $read; stopping capture")
                    return
                }
                continue
            }

            val rms = applyGainAndMeasure(samples, read)
            lastRms = rms

            if (!uplinkEnabled) {
                // Half-duplex. Drop the frame AND reset the hangover so the
                // model's own voice cannot leave the gate hanging open.
                hangoverFramesLeft = 0
                continue
            }

            if (!gateOpen(rms)) continue

            packLittleEndian(samples, read, outBytes)
            speechFramesSent++
            try {
                sink(if (read == samples.size) outBytes else outBytes.copyOf(read * 2))
            } catch (e: Exception) {
                Log.w(TAG, "audio sink threw", e)
            }
        }
    }

    /** Applies gain in place with saturation, and returns the resulting RMS. */
    private fun applyGainAndMeasure(samples: ShortArray, count: Int): Float {
        val g = gain
        var sumSquares = 0.0
        for (i in 0 until count) {
            val amplified =
                if (g == 1.0f) samples[i].toInt()
                else (samples[i] * g).toInt()
            // Clip rather than wrap: a wrapped sample is a loud click, and on a
            // device that needs heavy gain that would happen constantly.
            val clipped = amplified.coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt())
            samples[i] = clipped.toShort()
            sumSquares += (clipped.toDouble() * clipped.toDouble())
        }
        return sqrt(sumSquares / count).toFloat()
    }

    /**
     * Energy gate with a hangover tail, so the quiet end of a word is not
     * clipped off mid-sentence.
     */
    private fun gateOpen(rms: Float): Boolean {
        if (rms >= VAD_THRESHOLD_RMS) {
            hangoverFramesLeft = HANGOVER_FRAMES
            return true
        }
        if (hangoverFramesLeft > 0) {
            hangoverFramesLeft--
            return true
        }
        return false
    }

    private fun packLittleEndian(samples: ShortArray, count: Int, out: ByteArray) {
        var j = 0
        for (i in 0 until count) {
            val value = samples[i].toInt()
            out[j++] = (value and 0xFF).toByte()
            out[j++] = ((value shr 8) and 0xFF).toByte()
        }
    }

    companion object {
        private const val TAG = "EchoAudioCapture"

        /**
         * Frame RMS above which audio is streamed. Deliberately low -- the mic
         * is weak, and a false positive costs a few kB while a false negative
         * costs a missed sentence. Retune against the Phase 3 recording.
         */
        const val VAD_THRESHOLD_RMS = 420f

        /** 25 frames x 20 ms == 500 ms of tail after speech stops. */
        const val HANGOVER_FRAMES = 25
    }
}
