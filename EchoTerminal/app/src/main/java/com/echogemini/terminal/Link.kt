package com.echogemini.terminal

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.min
import kotlin.random.Random

/**
 * The one persistent outbound WebSocket (HANDOFF section 3).
 *
 * The device dials out and keeps a single socket open, multiplexing mic audio
 * up, camera frames up, model audio down and display commands down. No inbound
 * ports, no NAT problems, and reconnection logic lives in exactly one place --
 * this file.
 *
 * **The link never blocks the clock.** Every callback here is delivered on a
 * background thread and the ambient layer is driven independently; if this
 * class throws, hangs, or never connects at all, the Show is still a clock.
 * That is the explicit product requirement (HANDOFF section 8.1) and is what
 * verification step 12 tests by turning the server off.
 */
class Link(
    private val serverUrl: String,
    private val sharedSecret: String,
    private val deviceId: String,
    private val appVersion: String,
    private val listener: Listener
) {

    /** Everything the link reports. All calls arrive on OkHttp's reader thread. */
    interface Listener {
        fun onLinkState(state: State, detail: String)
        fun onControl(msg: Protocol.Incoming)
        fun onAudioDown(pcm: ByteArray)
    }

    enum class State { DISCONNECTED, CONNECTING, CONNECTED }

    @Volatile
    var state: State = State.DISCONNECTED
        private set

    private val client = OkHttpClient.Builder()
        // OkHttp's own ping keeps the TCP connection honest and detects a
        // half-open socket. The server ALSO sends application-level pings; both
        // are wanted, because they fail in different ways.
        .pingInterval(HEARTBEAT_SECONDS, TimeUnit.SECONDS)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)  // a quiet link is a healthy link
        .retryOnConnectionFailure(true)
        .build()

    @Volatile
    private var socket: WebSocket? = null

    @Volatile
    private var running = false

    @Volatile
    private var attempt = 0

    private val seqAudio = AtomicInteger(0)
    private val seqVideo = AtomicInteger(0)

    private val reconnectThread = Thread(::reconnectLoop, "echo-link-reconnect").apply {
        isDaemon = true
    }

    /** Released to cut a backoff sleep short when the socket drops. */
    private val wake = Semaphore(0)

    // --- lifecycle ----------------------------------------------------------

    fun start() {
        if (running) return
        running = true
        reconnectThread.start()
    }

    fun stop() {
        running = false
        wake.release()
        socket?.close(1000, "client stopping")
        socket = null
        setState(State.DISCONNECTED, "stopped")
    }

    /**
     * Reconnect with exponential backoff plus jitter.
     *
     * The Windows PC being off is a NORMAL state, not an error: the device is
     * expected to sit here retrying for hours while remaining a perfectly good
     * clock. Hence the cap -- there is no value in hammering a machine that is
     * switched off, and a 1 GB device has better things to do.
     */
    private fun reconnectLoop() {
        while (running) {
            if (state == State.DISCONNECTED) {
                connect()
            }
            try {
                // Returns early if something released the semaphore -- i.e. the
                // socket dropped and we should retry now rather than idle out
                // the rest of the backoff.
                wake.tryAcquire(backoffMillis(attempt), TimeUnit.MILLISECONDS)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return
            }
        }
    }

    private fun backoffMillis(attempt: Int): Long {
        if (attempt <= 0) return BASE_BACKOFF_MS
        val exponential = BASE_BACKOFF_MS shl min(attempt, 6)   // 1s .. 64s
        val capped = min(exponential, MAX_BACKOFF_MS)
        // Jitter so a Wi-Fi blip that drops several things at once does not
        // produce a synchronised retry storm.
        return capped / 2 + Random.nextLong(capped / 2 + 1)
    }

    private fun connect() {
        if (sharedSecret.isEmpty()) {
            // Fail loudly in the log but keep the app alive -- the clock does
            // not care, and a misconfigured build should be obvious in logcat
            // rather than silently never connecting.
            Log.e(TAG, "SHARED_SECRET is empty; set echo.sharedSecret in local.properties")
            setState(State.DISCONNECTED, "no shared secret configured")
            attempt++
            return
        }

        setState(State.CONNECTING, "dialling $serverUrl")
        val request = Request.Builder()
            .url(serverUrl.toHttpForm())
            .header(SECRET_HEADER, sharedSecret)
            .build()
        socket = client.newWebSocket(request, SocketListener())
    }

    // --- sending ------------------------------------------------------------

    /** Returns false if the frame was dropped because the link is down. */
    fun sendAudio(pcm: ByteArray): Boolean =
        sendFrame(Protocol.Channel.AUDIO_UP, seqAudio.getAndIncrement(), pcm)

    fun sendVideo(jpeg: ByteArray): Boolean =
        sendFrame(Protocol.Channel.VIDEO_UP, seqVideo.getAndIncrement(), jpeg)

    private fun sendFrame(channel: Byte, seq: Int, payload: ByteArray): Boolean {
        val ws = socket ?: return false
        if (state != State.CONNECTED) return false
        return try {
            ws.send(Protocol.encodeFrame(channel, seq, payload).toByteString())
        } catch (e: Exception) {
            Log.w(TAG, "send failed on channel $channel", e)
            false
        }
    }

    fun sendControl(json: String): Boolean {
        val ws = socket ?: return false
        if (state != State.CONNECTED) return false
        return try {
            ws.send(json)
        } catch (e: Exception) {
            Log.w(TAG, "control send failed", e)
            false
        }
    }

    fun sendTap() = sendControl(Protocol.tap(true))

    fun sendCameraStatus(status: Protocol.CameraStatus, detail: String = "") =
        sendControl(Protocol.cameraStatus(status, detail))

    // --- socket callbacks ---------------------------------------------------

    private inner class SocketListener : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            attempt = 0
            setState(State.CONNECTED, "connected")
            seqAudio.set(0)
            seqVideo.set(0)
            val capabilities = JSONObject().apply {
                put("microphone", true)
                put("camera", true)
                put("display", "960x480")
                put("tap_to_talk", true)
                put("alarms", true)
                put("timers", true)
            }
            webSocket.send(Protocol.hello(deviceId, appVersion, capabilities))
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val msg = Protocol.decode(text) ?: return
            if (msg.type == Protocol.Type.PING) {
                webSocket.send(Protocol.pong(msg.int("nonce")))
                return
            }
            try {
                listener.onControl(msg)
            } catch (e: Exception) {
                // A bad display payload must never take the link -- or the
                // clock -- down with it.
                Log.e(TAG, "listener threw handling ${msg.type}", e)
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            val frame = Protocol.decodeFrame(bytes.toByteArray()) ?: return
            when (frame.channel) {
                Protocol.Channel.AUDIO_DOWN -> listener.onAudioDown(frame.payload)
                else -> Log.w(TAG, "unexpected media on channel ${frame.channel}")
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            Log.i(TAG, "server closing link: $code $reason")
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            attempt++
            setState(State.DISCONNECTED, "closed: $code $reason")
            nudgeReconnect()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            attempt++
            val detail = when (response?.code) {
                401 -> "rejected: shared secret does not match the server"
                else -> t.message ?: t.javaClass.simpleName
            }
            // Log at INFO, not ERROR: with the PC switched off this fires every
            // backoff interval, forever, and it is not a fault.
            Log.i(TAG, "link down ($detail); retry in ${backoffMillis(attempt)} ms")
            setState(State.DISCONNECTED, detail)
            nudgeReconnect()
        }
    }

    private fun nudgeReconnect() {
        socket = null
        wake.release()
    }

    private fun setState(next: State, detail: String) {
        if (state == next) return
        state = next
        try {
            listener.onLinkState(next, detail)
        } catch (e: Exception) {
            Log.e(TAG, "listener threw on state $next", e)
        }
    }

    companion object {
        private const val TAG = "EchoLink"
        const val SECRET_HEADER = "X-Echo-Secret"
        private const val HEARTBEAT_SECONDS = 15L
        private const val BASE_BACKOFF_MS = 1_000L
        private const val MAX_BACKOFF_MS = 60_000L

        /** OkHttp wants http(s) scheme URLs even for WebSockets. */
        private fun String.toHttpForm(): String = when {
            startsWith("ws://") -> "http://" + substring(5)
            startsWith("wss://") -> "https://" + substring(6)
            else -> this
        }
    }
}
