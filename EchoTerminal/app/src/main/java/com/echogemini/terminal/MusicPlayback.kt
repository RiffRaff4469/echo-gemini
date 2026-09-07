package com.echogemini.terminal

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Log
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Speaker playback for music, 24 kHz mono PCM16 (protocol v1.5).
 *
 * Same format as [AudioPlayback], same speaker, and a completely separate
 * object -- which is the point. [AudioPlayback] exists to carry *the assistant's
 * voice*, and everything it does follows from that: it reports speaking state,
 * which closes the microphone uplink so the model does not hear itself
 * (half-duplex, HANDOFF 8.2), and barge-in flushes it so an abandoned answer
 * stops immediately.
 *
 * Every one of those behaviours is wrong for music. Reporting speaking would
 * hold the mic shut for as long as an album played, so there would be no wake
 * word and therefore no way to say "pause". Flushing on barge-in would stop the
 * music every time somebody spoke over the model. And the two streams would be
 * interleaved into one AudioTrack, which sounds exactly as bad as it reads.
 *
 * So: two tracks, tagged differently, mixed by Android. This one is
 * USAGE_MEDIA/CONTENT_TYPE_MUSIC, so the platform ducks and routes it as music.
 * Whether music and speech are ever audible at the same time is a *server*
 * decision -- see the holds in `server/spotify.py` -- and not something this
 * class second-guesses.
 *
 * The server owns the transport. There is no pause here, because a pause here
 * would only stop the sound while the PC kept decoding and the track kept
 * advancing; [flush] is for dropping audio that is already stale, not for
 * pausing.
 */
class MusicPlayback {

    @Volatile
    private var running = false

    private var track: AudioTrack? = null
    private var thread: Thread? = null

    /**
     * Deeper than the voice queue: this is a continuous stream rather than a
     * few seconds of answer, and the server drops the OLDEST chunk under
     * pressure anyway. Still bounded -- on a 1 GB device an unbounded queue in
     * front of a stalled AudioTrack is a slow crash.
     */
    private val queue = ArrayBlockingQueue<ByteArray>(QUEUE_CAPACITY)

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

        // Roughly half a second, four times the voice buffer. Music has no
        // barge-in latency requirement -- nobody is waiting for it to stop
        // mid-word -- and a bigger buffer is what rides out a Wi-Fi hiccup
        // without a dropout.
        val bufferBytes = maxOf(minBuffer, Protocol.AUDIO_DOWN_RATE)

        val built = try {
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
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
                .build()
        } catch (e: Exception) {
            Log.e(TAG, "could not construct the music AudioTrack", e)
            return false
        }

        if (built.state != AudioTrack.STATE_INITIALIZED) {
            Log.e(TAG, "music AudioTrack failed to initialise (state=${built.state})")
            built.release()
            return false
        }

        track = built
        running = true
        built.play()
        thread = Thread(::loop, "echo-music-playback").apply {
            // Below the voice track's priority: if the device is short of CPU,
            // the answer to a question matters more than a gapless album.
            priority = Thread.NORM_PRIORITY + 1
            start()
        }
        Log.i(TAG, "music playback started: ${Protocol.AUDIO_DOWN_RATE} Hz mono PCM16")
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
                Log.w(TAG, "music AudioTrack teardown threw", e)
            }
            it.release()
        }
        track = null
    }

    fun enqueue(pcm: ByteArray) {
        if (!running || pcm.isEmpty()) return
        if (!queue.offer(pcm)) {
            // The server is sending faster than this can play, which means the
            // link caught up after a stall. Dropping the newest keeps what is
            // already queued in order.
            dropped++
            if (dropped % 100 == 1L) {
                Log.w(TAG, "music queue full; dropped $dropped chunks")
            }
        }
    }

    /**
     * Drop everything buffered. Used when the link goes down, not for pausing:
     * audio queued from a server that is no longer there is by definition the
     * past, and playing thirty seconds of it after a reconnect is worse than
     * silence.
     */
    fun flush() {
        generation++
        queue.clear()
        track?.let {
            try {
                it.pause()
                it.flush()
                it.play()
            } catch (e: IllegalStateException) {
                Log.w(TAG, "music flush threw", e)
            }
        }
    }

    private fun loop() {
        while (running) {
            val myGeneration = generation
            val chunk = try {
                queue.poll(200, TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return
            } ?: continue

            if (myGeneration != generation) continue

            val output = track ?: return
            var offset = 0
            while (offset < chunk.size && running && myGeneration == generation) {
                val written = try {
                    output.write(chunk, offset, chunk.size - offset)
                } catch (e: IllegalStateException) {
                    Log.w(TAG, "music AudioTrack.write threw", e)
                    return
                }
                if (written < 0) {
                    Log.e(TAG, "music AudioTrack.write error $written")
                    return
                }
                offset += written
            }
        }
    }

    @Volatile
    private var dropped = 0L

    companion object {
        private const val TAG = "EchoMusicPlayback"

        /** ~3 s of 50 ms chunks: enough to absorb a hiccup, far short of a leak. */
        private const val QUEUE_CAPACITY = 60
    }
}
