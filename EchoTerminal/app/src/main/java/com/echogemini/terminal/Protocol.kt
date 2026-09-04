package com.echogemini.terminal

import android.util.Log
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Hand-mirror of `server/protocol.py`. Read that file first -- it is the
 * authority, and anything changed there must be changed here in the same
 * commit.
 *
 * Text frames are JSON control messages in a versioned envelope:
 * `{"v":1,"t":"<type>","ts":<epoch_ms>, ...}`.
 *
 * Binary frames are media, with an 8-byte big-endian header so audio up, audio
 * down and video up share the one socket without a base64 tax on every 20 ms of
 * PCM:
 *
 * ```
 * offset size field
 * 0      1    version
 * 1      1    channel
 * 2      2    flags (reserved, 0)
 * 4      4    seq   (per-channel, wraps)
 * 8      ...  payload
 * ```
 */
object Protocol {

    const val VERSION = 1

    // Gemini Live: 16 kHz in, 24 kHz out, PCM16 mono little-endian. The device
    // produces and consumes exactly these so the server never resamples.
    const val AUDIO_UP_RATE = 16_000
    const val AUDIO_DOWN_RATE = 24_000
    const val AUDIO_FRAME_MS = 20
    const val AUDIO_UP_FRAME_SAMPLES = AUDIO_UP_RATE * AUDIO_FRAME_MS / 1000 // 320
    const val AUDIO_UP_FRAME_BYTES = AUDIO_UP_FRAME_SAMPLES * 2              // 640

    // Video in: JPEG, max 1 FPS, 768x768 recommended.
    const val VIDEO_EDGE = 768
    const val VIDEO_MAX_FPS = 1.0

    const val HEADER_SIZE = 8

    object Channel {
        const val AUDIO_UP: Byte = 0x01
        const val AUDIO_DOWN: Byte = 0x02
        const val VIDEO_UP: Byte = 0x03
    }

    /** Message type strings, device -> server and server -> device. */
    object Type {
        const val HELLO = "hello"
        const val PONG = "pong"
        const val TAP = "tap"
        const val CAMERA_STATUS = "camera_status"
        const val DEVICE_LOG = "device_log"
        const val ERROR = "error"

        const val WELCOME = "welcome"
        const val PING = "ping"
        const val STATE = "state"
        const val MIC = "mic"
        const val INTERRUPT = "interrupt"
        const val DISPLAY = "display"
        const val DISPLAY_CLEAR = "display_clear"
        const val VIDEO = "video"
    }

    /** Server-driven UI state. */
    enum class UiState(val wire: String) {
        IDLE("idle"),
        LISTENING("listening"),
        THINKING("thinking"),
        SPEAKING("speaking");

        companion object {
            fun from(wire: String?): UiState =
                values().firstOrNull { it.wire == wire } ?: IDLE
        }
    }

    /** What the single camera owner is doing, reported upward. */
    enum class CameraStatus(val wire: String) {
        RELEASED("released"),
        OPENING("opening"),
        STREAMING("streaming"),
        SHUTTER_CLOSED("shutter_closed"),
        ERROR("error")
    }

    enum class DisplayType(val wire: String) {
        TEXT("text"),
        HTML("html"),
        IMAGE("image"),
        TIMER("timer");

        companion object {
            fun from(wire: String?): DisplayType? = values().firstOrNull { it.wire == wire }
        }
    }

    // --- outbound envelopes -------------------------------------------------

    private fun envelope(type: String): JSONObject = JSONObject().apply {
        put("v", VERSION)
        put("t", type)
        put("ts", System.currentTimeMillis())
    }

    fun hello(deviceId: String, appVersion: String, capabilities: JSONObject): String =
        envelope(Type.HELLO).apply {
            put("device_id", deviceId)
            put("app_version", appVersion)
            put("protocol_version", VERSION)
            put("capabilities", capabilities)
        }.toString()

    fun pong(nonce: Int): String = envelope(Type.PONG).apply { put("nonce", nonce) }.toString()

    fun tap(pressed: Boolean = true): String =
        envelope(Type.TAP).apply { put("pressed", pressed) }.toString()

    fun cameraStatus(status: CameraStatus, detail: String = ""): String =
        envelope(Type.CAMERA_STATUS).apply {
            put("status", status.wire)
            put("detail", detail)
        }.toString()

    fun deviceLog(level: String, message: String): String =
        envelope(Type.DEVICE_LOG).apply {
            put("level", level)
            put("message", message)
        }.toString()

    fun error(code: String, message: String): String =
        envelope(Type.ERROR).apply {
            put("code", code)
            put("message", message)
        }.toString()

    // --- inbound ------------------------------------------------------------

    /**
     * A decoded control message. Kept as a thin wrapper over [JSONObject]
     * rather than a sealed hierarchy: the client only ever reads a handful of
     * fields, and an unknown message must be ignorable, never fatal.
     */
    class Incoming(val type: String, val body: JSONObject) {
        fun int(key: String, fallback: Int = 0): Int = body.optInt(key, fallback)
        fun bool(key: String, fallback: Boolean = false): Boolean = body.optBoolean(key, fallback)
        fun str(key: String, fallback: String = ""): String = body.optString(key, fallback)
        fun dbl(key: String, fallback: Double = 0.0): Double = body.optDouble(key, fallback)
        fun obj(key: String): JSONObject? = body.optJSONObject(key)
    }

    /** Returns null for anything unparseable or from a different protocol version. */
    fun decode(text: String): Incoming? = try {
        val body = JSONObject(text)
        val version = body.optInt("v", -1)
        when {
            version != VERSION -> {
                Log.w(TAG, "ignoring message with protocol v$version (we speak v$VERSION)")
                null
            }
            else -> Incoming(body.optString("t"), body)
        }
    } catch (e: Exception) {
        Log.w(TAG, "undecodable control message: ${text.take(160)}", e)
        null
    }

    /** A pushed display command: `{type, payload, duration, priority}`. */
    data class DisplayCommand(
        val type: DisplayType,
        val payload: JSONObject,
        val durationMs: Long,
        val priority: Int
    ) {
        companion object {
            fun from(msg: Incoming): DisplayCommand? {
                val type = DisplayType.from(msg.str("type")) ?: run {
                    Log.w("EchoProtocol", "unknown display type: ${msg.str("type")}")
                    return null
                }
                val payload = msg.obj("payload") ?: return null
                val seconds = msg.dbl("duration", 0.0)
                return DisplayCommand(
                    type = type,
                    payload = payload,
                    durationMs = if (seconds.isNaN() || seconds <= 0) 0L else (seconds * 1000).toLong(),
                    priority = msg.int("priority", 0)
                )
            }
        }
    }

    // --- binary media frames ------------------------------------------------

    fun encodeFrame(channel: Byte, seq: Int, payload: ByteArray): ByteArray {
        val buffer = ByteBuffer.allocate(HEADER_SIZE + payload.size).order(ByteOrder.BIG_ENDIAN)
        buffer.put(VERSION.toByte())
        buffer.put(channel)
        buffer.putShort(0)          // flags, reserved
        buffer.putInt(seq)
        buffer.put(payload)
        return buffer.array()
    }

    class MediaFrame(val channel: Byte, val seq: Int, val payload: ByteArray)

    fun decodeFrame(raw: ByteArray): MediaFrame? {
        if (raw.size < HEADER_SIZE) {
            Log.w(TAG, "binary frame too short: ${raw.size}")
            return null
        }
        val buffer = ByteBuffer.wrap(raw).order(ByteOrder.BIG_ENDIAN)
        val version = buffer.get()
        if (version.toInt() != VERSION) {
            Log.w(TAG, "binary frame version ${version.toInt()} != $VERSION")
            return null
        }
        val channel = buffer.get()
        buffer.short                 // flags, ignored
        val seq = buffer.int
        val payload = ByteArray(raw.size - HEADER_SIZE)
        buffer.get(payload)
        return MediaFrame(channel, seq, payload)
    }

    private const val TAG = "EchoProtocol"
}
