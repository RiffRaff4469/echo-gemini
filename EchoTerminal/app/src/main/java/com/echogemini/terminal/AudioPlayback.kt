package com.echogemini.terminal

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Speaker playback: 24 kHz mono PCM16, straight from the Live API.
 *
 * The rate is not a choice -- the Live API emits 24 kHz audio out (HANDOFF
 * section 13) and the server passes it through without resampling, so this is
 * where it lands.
 *
 * **Barge-in is the interesting part.** When the user talks over the model, the
 * server sends `interrupt` and everything already queued here is instantly
 * wrong: it is the answer to a question that has been abandoned. [flush] has to
 * drop it immediately, not fade it out, or the device keeps talking over the
 * person for the second or two of audio still buffered.
 */
class AudioPlayback {

    /** Called when playback starts and stops, so the UI can show SPEAKING. */
    var onSpeakingChanged: ((Boolean) -> Unit)? = null

    @Volatile
    private var running = false

    @Volatile
    private var speaking = false

    private var track: AudioTrack? = null
    private var thread: Thread? = null

    /**
     * Bounded: on a 1 GB device an unbounded queue in front of a stalled
     * AudioTrack is a slow crash. Dropping the newest chunk when full is right
     * here (unlike the mic uplink, where the freshest audio is what matters) --
     * the model's reply must stay in order, so a gap at the end is better than
     * a gap in the middle.
     */
    private val queue = ArrayBlockingQueue<ByteArray>(QUEUE_CAPACITY)

    /** Bumped on every flush; the writer drops chunks from older generations. */
    @Volatile
    private var generation = 0

    fun start(): Boolean {
        if (running) return true

        val minBuffer = AudioTrack.getMinBufferSize(
            Protocol.AUDIO_DOWN_RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuffer <= 0) {
            Log.e(TAG, "AudioTrack.getMinBufferSize returned $minBuffer")
            return false
        }

        // Small buffer on purpose: a big one is more latency between the
        // interrupt arriving and the room going quiet.
        val bufferBytes = maxOf(minBuffer, Protocol.AUDIO_DOWN_RATE * 2 / 5) // ~200 ms

        val built = try {
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(Protocol.AUDIO_DOWN_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build()
                )
                .setBufferSizeInBytes(bufferBytes)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                .build()
        } catch (e: Exception) {
            Log.e(TAG, "could not construct AudioTrack", e)
            return false
        }

        if (built.state != AudioTrack.STATE_INITIALIZED) {
            Log.e(TAG, "AudioTrack failed to initialise (state=${built.state})")
            built.release()
            return false
        }

        track = built
        running = true
        built.play()
        thread = Thread(::loop, "echo-audio-playback").apply {
            priority = Thread.MAX_PRIORITY - 1
            start()
        }
        Log.i(TAG, "playback started: ${Protocol.AUDIO_DOWN_RATE} Hz mono PCM16")
        return true
    }

    fun stop() {
        running = false
        queue.clear()
        thread?.interrupt()
        thread?.join(500)
        thread = null
        track?.let {
            try {
                it.pause()
                it.flush()
                it.stop()
            } catch (e: IllegalStateException) {
                Log.w(TAG, "AudioTrack teardown threw", e)
            }
            it.release()
        }
        track = null
        setSpeaking(false)
    }

    /** Enqueue a chunk of model audio for playback. */
    fun enqueue(pcm: ByteArray) {
        if (!running || pcm.isEmpty()) return
        if (!queue.offer(pcm)) {
            Log.w(TAG, "playback queue full; dropping ${pcm.size} bytes")
        }
    }

    /**
     * Barge-in. Drop everything queued AND everything already handed to the
     * hardware, right now.
     *
     * [AudioTrack.pause] then [AudioTrack.flush] is the order that actually
     * discards the hardware buffer -- flushing a playing track is a no-op, and
     * getting this wrong means the device talks over the user for another
     * second or two, which is precisely the failure barge-in exists to prevent.
     */
    fun flush() {
        val cleared = queue.size
        generation++
        queue.clear()
        track?.let {
            try {
                it.pause()
                it.flush()
                it.play()
            } catch (e: IllegalStateException) {
                Log.w(TAG, "flush threw", e)
            }
        }
        setSpeaking(false)
        Log.i(TAG, "flushed playback (barge-in), dropped $cleared queued chunks")
    }

    private fun loop() {
        while (running) {
            val myGeneration = generation
            val chunk = try {
                queue.poll(200, TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return
            }

            if (chunk == null) {
                // Nothing queued for 200 ms: the turn is over.
                if (speaking) setSpeaking(false)
                continue
            }

            // A flush between poll and write means this chunk belongs to an
            // abandoned answer. Drop it rather than play a fragment.
            if (myGeneration != generation) continue

            if (!speaking) setSpeaking(true)

            val output = track ?: return
            var offset = 0
            while (offset < chunk.size && running && myGeneration == generation) {
                val written = try {
                    output.write(chunk, offset, chunk.size - offset)
                } catch (e: IllegalStateException) {
                    Log.w(TAG, "AudioTrack.write threw", e)
                    return
                }
                if (written < 0) {
                    Log.e(TAG, "AudioTrack.write error $written")
                    return
                }
                offset += written
            }
        }
    }

    private fun setSpeaking(value: Boolean) {
        if (speaking == value) return
        speaking = value
        onSpeakingChanged?.invoke(value)
    }

    companion object {
        private const val TAG = "EchoAudioPlayback"

        /** ~40 chunks of model audio; several seconds, well short of a leak. */
        private const val QUEUE_CAPACITY = 40
    }
}
