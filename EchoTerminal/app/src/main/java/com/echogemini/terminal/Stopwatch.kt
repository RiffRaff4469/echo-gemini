package com.echogemini.terminal

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject

/**
 * Device-owned stopwatch (CLOCK-BRIEF-STOPWATCH). Mirrors [AlarmScheduler]: a
 * main-thread singleton, SharedPreferences-backed, working with the server off.
 *
 * The elapsed time is accumulated in [elapsedMs] plus the live [baseRealtimeMs]
 * delta while running, so ticks only repaint -- nothing accumulates on the
 * ticker. A partial wake lock keeps the process alive through screen-off; the
 * elapsed persists so a killed process resumes paused at the persisted value.
 */
class Stopwatch private constructor(private val context: Context) {
    companion object {
        @Volatile private var instance: Stopwatch? = null
        fun get(context: Context): Stopwatch =
            instance ?: synchronized(this) {
                instance ?: Stopwatch(context.applicationContext).also { instance = it }
            }
    }

    private val tag = "EchoStopwatch"
    private val prefs = context.createDeviceProtectedStorageContext()
        .getSharedPreferences("stopwatch", Context.MODE_PRIVATE)
    private val handler = Handler(Looper.getMainLooper())
    private val wake = context.getSystemService(PowerManager::class.java)
        .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "EchoTerminal:stopwatch")
        .apply { setReferenceCounted(false) }

    /** UI mirror repaint; fires on every tick and transition. */
    var onChanged: (() -> Unit)? = null

    /** Push hook: fires the full stopwatch_state control string upward on every
     *  transition (never on ticks -- the server is not a tick consumer). */
    var onState: ((String) -> Unit)? = null

    /** Set by [AlarmService] once it is hosting us (same contract as the
     *  scheduler's) so the foreground service stops itself when nothing needs
     *  it. */
    var onServiceChanged: (() -> Unit)? = null

    var running = false
        private set

    /** Accumulated elapsed at the last pause/reset (ms). */
    private var elapsedMs = 0L

    /** [SystemClock.elapsedRealtime] when running started/resumed. */
    private var baseRealtimeMs = 0L

    private val lapList = mutableListOf<Long>()
    private var persisted = false

    val laps: List<Long> get() = lapList.toList()

    /** Live elapsed while running, else the paused value. */
    fun elapsed(): Long =
        if (running) elapsedMs + (SystemClock.elapsedRealtime() - baseRealtimeMs) else elapsedMs

    init {
        elapsedMs = prefs.getLong("elapsed_ms", 0L)
        val saved = prefs.getString("laps", "[]")
        try {
            val arr = JSONArray(saved)
            for (i in 0 until arr.length()) lapList.add(arr.getLong(i))
        } catch (e: Exception) {
            Log.e(tag, "Cannot restore laps", e)
        }
        // A process death while running resumes paused at the persisted value:
        // honest, and never silently wrong about how long it has been counting.
        running = false
        if (elapsedMs > 0 || lapList.isNotEmpty()) onState?.invoke(snapshot())
    }

    // --- transitions ---------------------------------------------------------

    fun start() {
        if (running) return
        baseRealtimeMs = SystemClock.elapsedRealtime()
        running = true
        wake.acquire()
        persist()
        push()
        tick()
        if (onServiceChanged == null) {
            try {
                context.startForegroundService(
                    android.content.Intent(context, AlarmService::class.java)
                )
            } catch (e: Exception) {
                Log.e(tag, "Cannot start alarm service for the stopwatch", e)
            }
        }
    }

    fun pause() {
        if (!running) return
        elapsedMs = elapsed()
        running = false
        baseRealtimeMs = 0L
        if (wake.isHeld) wake.release()
        persist()
        push()
    }

    fun lap() {
        if (!running) return
        lapList.add(elapsed())
        persist()
        push()
    }

    fun reset() {
        val wasRunning = running
        if (wasRunning) pause()
        elapsedMs = 0L
        lapList.clear()
        persist()
        push()
    }

    /** The wire snapshot: a full control envelope for the server. */
    fun snapshot(reqId: String = ""): String {
        val laps = JSONArray()
        for (lap in lapList) laps.put(lap)
        return Protocol.envelope(Protocol.Type.STOPWATCH_STATE).apply {
            put("running", running)
            put("elapsed_ms", elapsed())
            put("laps_ms", laps)
            if (reqId.isNotEmpty()) put("req_id", reqId)
        }.toString()
    }

    /**
     * Apply one server op (a voice intent relayed over `alarm_command`).
     * Returns the ack snapshot the server waits on. Mirrors
     * [AlarmScheduler.command]'s contract.
     */
    fun command(cmd: JSONObject): String {
        check(Looper.myLooper() == Looper.getMainLooper())
        val op = cmd.optString("op", "")
        when (op) {
            "stopwatch_start" -> start()
            "stopwatch_pause" -> pause()
            "stopwatch_reset" -> reset()
            "stopwatch_lap" -> lap()
            "stopwatch_status" -> Unit // ack with the current snapshot
            else -> return snapshot(cmd.optString("req_id"))
        }
        return snapshot(cmd.optString("req_id"))
    }

    // --- internals -----------------------------------------------------------

    private val tick = object : Runnable {
        override fun run() {
            if (!running) return
            onChanged?.invoke()
            // Persist once a second so a kill loses at most a second.
            if (SystemClock.elapsedRealtime() - baseRealtimeMs - elapsedMs >= 1_000L ||
                !persisted
            ) persist()
            handler.postDelayed(this, 100)
        }
    }

    private fun tick() {
        handler.removeCallbacks(this.tick)
        onChanged?.invoke()
        if (running) handler.postDelayed(this.tick, 100)
    }

    private fun persist() {
        persisted = true
        prefs.edit()
            .putLong("elapsed_ms", if (running) elapsed() else elapsedMs)
            .putString("laps", JSONArray(lapList).toString())
            .commit()
    }

    private fun push() {
        onChanged?.invoke()
        onState?.invoke(snapshot())
    }
}
