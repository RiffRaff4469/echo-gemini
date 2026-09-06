package com.echogemini.terminal

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** Application-owned, main-thread scheduler. No server is needed for any operation. */
class AlarmScheduler private constructor(private val context: Context) {
    private val prefs = context.createDeviceProtectedStorageContext()
        .getSharedPreferences("schedule", Context.MODE_PRIVATE)
    private val manager = context.getSystemService(AlarmManager::class.java)
    private val handler = Handler(Looper.getMainLooper())
    private val boot get() = Settings.Global.getInt(context.contentResolver, Settings.Global.BOOT_COUNT, 0)
    private val entries = mutableListOf<JSONObject>()
    private val wake = context.getSystemService(PowerManager::class.java)
        .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "EchoTerminal:timers").apply { setReferenceCounted(false) }
    var onChanged: (() -> Unit)? = null
    var onFired: ((JSONObject) -> Unit)? = null
    var onState: (() -> Unit)? = null
    @Volatile var isRinging = false
        private set
    var onServiceChanged: (() -> Unit)? = null
    val exactAllowed get() = Build.VERSION.SDK_INT < 31 || manager.canScheduleExactAlarms()
    val ringing get() = entries.filter { it.optBoolean("ringing") }
    val activeTimers get() = entries.any { it.optString("kind") == "timer" && it.optBoolean("enabled") }
    val needsService get() = activeTimers || ringing.isNotEmpty()

    init {
        try {
            val saved = JSONArray(prefs.getString("entries", "[]"))
            for (i in 0 until saved.length()) entries.add(saved.getJSONObject(i))
        } catch (e: Exception) { Log.e("EchoSchedule", "Cannot restore schedule", e) }
    }

    private fun remaining(e: JSONObject): Long = ScheduleTime.remaining(
        e.optLong("deadline_elapsed"), e.optLong("deadline_epoch"), e.optInt("boot"),
        boot, SystemClock.elapsedRealtime(), System.currentTimeMillis())

    fun snapshot(reqId: String = "", error: String = ""): String {
        val alarms = JSONArray()
        val timers = JSONArray()
        entries.forEach { e ->
            val copy = JSONObject(e.toString())
            if (e.getString("kind") == "timer") {
                copy.put("remaining_s", remaining(e) / 1000.0)
                timers.put(copy)
            } else alarms.put(copy)
        }
        return Protocol.envelope(Protocol.Type.ALARM_STATE).put("alarms", alarms)
            .put("timers", timers).put("req_id", reqId).put("error", error)
            .put("exact_allowed", exactAllowed).toString()
    }

    /** Acks only after synchronous persistence and registration with AlarmManager. */
    fun command(cmd: JSONObject): String {
        check(Looper.myLooper() == Looper.getMainLooper())
        var error = ""
        try {
            when (cmd.getString("op")) {
                "set_alarm", "set_timer" -> {
                    val timer = cmd.getString("op") == "set_timer"
                    val duration = cmd.optDouble("duration_s", 0.0)
                    val epoch = cmd.optLong("time_epoch_ms")
                    require(!timer || (duration.isFinite() && duration >= 0.001 && duration <= 86400)) {
                        "Timer duration must be between 0.001 and 86400 seconds"
                    }
                    require(timer || epoch > System.currentTimeMillis()) { "Alarm must be in the future" }
                    val days = cmd.optJSONArray("days") ?: JSONArray()
                    for (i in 0 until days.length()) require(days.getString(i) in ScheduleTime.days) { "Invalid weekday" }
                    val id = cmd.optString("id").ifEmpty { UUID.randomUUID().toString() }
                    require(entries.none { it.getString("id") == id }) { "ID already exists; cancel it first" }
                    require(entries.size < 100) { "Schedule is full" }
                    val e = JSONObject().put("id", id).put("kind", if (timer) "timer" else "alarm")
                        .put("label", cmd.optString("label").take(64)).put("days", days)
                        .put("time_epoch_ms", epoch).put("duration_s", if (timer) duration else 0)
                        .put("enabled", true).put("ringing", false)
                    if (!timer) {
                        val local = java.time.Instant.ofEpochMilli(epoch).atZone(java.time.ZoneId.systemDefault())
                        e.put("clock_hour", local.hour).put("clock_minute", local.minute)
                    }
                    if (timer) {
                        e.put("deadline_elapsed", SystemClock.elapsedRealtime() + (duration * 1000).toLong())
                            .put("deadline_epoch", System.currentTimeMillis() + (duration * 1000).toLong())
                            .put("boot", boot)
                    }
                    // Normalize first occurrence to the requested weekdays too.
                    if (!timer && days.length() > 0) e.put("time_epoch_ms", ScheduleTime.nextRepeat(
                        epoch, (0 until days.length()).map { days.getString(it) }, System.currentTimeMillis()))
                    entries.add(e)
                    try { save(); schedule(e) } catch (ex: Exception) {
                        entries.remove(e); manager.cancel(pending(id)); save(); throw ex
                    }
                }
                "cancel" -> {
                    val id = cmd.optString("id")
                    val label = cmd.optString("label")
                    val kind = cmd.optString("kind")
                    require(kind.isEmpty() || kind in listOf("alarm", "timer")) { "Invalid kind" }
                    val matches = entries.filter {
                        (kind.isEmpty() || it.getString("kind") == kind) &&
                            (if (id.isNotEmpty()) it.getString("id") == id
                             else label.isEmpty() || it.optString("label").equals(label, true))
                    }
                    require(matches.size == 1) { if (matches.isEmpty()) "No matching entry" else "Please specify which one" }
                    manager.cancel(pending(matches.single().getString("id")))
                    entries.remove(matches.single())
                    save()
                }
                "list" -> Unit
                else -> error("Unknown alarm operation")
            }
        } catch (e: Exception) { error = e.message ?: "Could not update schedule" }
        refreshRuntime()
        return snapshot(cmd.optString("req_id"), error)
    }

    private fun pending(id: String): PendingIntent = PendingIntent.getBroadcast(context, 0,
        Intent(context, AlarmReceiver::class.java).setAction("com.echogemini.ALARM")
            .setData(Uri.Builder().scheme("echo-alarm").authority("local").appendPath(id).build()),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    private fun schedule(e: JSONObject) {
        if (!e.optBoolean("enabled")) return
        val timer = e.getString("kind") == "timer"
        val type = if (timer) AlarmManager.ELAPSED_REALTIME_WAKEUP else AlarmManager.RTC_WAKEUP
        val at = if (timer) SystemClock.elapsedRealtime() + remaining(e) else e.getLong("time_epoch_ms")
        val pi = pending(e.getString("id"))
        if (exactAllowed) {
            try { manager.setExactAndAllowWhileIdle(type, at, pi); return }
            catch (_: SecurityException) { /* Permission revoked: retain an inexact fallback. */ }
        }
        manager.setAndAllowWhileIdle(type, at, pi)
    }

    fun restore() {
        // Rebase timer deadlines after reboot and preserve elapsed-time countdowns
        // when the user changes the wall clock. Store once, never on every tick.
        entries.filter { it.getString("kind") == "timer" && it.optBoolean("enabled") }.forEach {
            val left = remaining(it)
            it.put("deadline_elapsed", SystemClock.elapsedRealtime() + left)
                .put("deadline_epoch", System.currentTimeMillis() + left).put("boot", boot)
        }
        save()
        entries.forEach { schedule(it) }
        tickDue()
        refreshRuntime()
    }

    fun fire(id: String) {
        val e = entries.firstOrNull { it.getString("id") == id } ?: return
        if (!e.optBoolean("enabled")) return
        val timer = e.getString("kind") == "timer"
        if (if (timer) remaining(e) > 0 else e.getLong("time_epoch_ms") > System.currentTimeMillis()) return
        e.put("ringing", true)
        val days = e.optJSONArray("days") ?: JSONArray()
        if (!timer && days.length() > 0) {
            e.put("time_epoch_ms", ScheduleTime.nextRepeatAt(e.getInt("clock_hour"), e.getInt("clock_minute"),
                (0 until days.length()).map { days.getString(it) }, System.currentTimeMillis()))
            schedule(e)
        } else e.put("enabled", false)
        save()
        refreshRuntime() // Local service/chime starts regardless of the listener/socket.
        onFired?.invoke(Protocol.envelope(Protocol.Type.ALARM_FIRED)
            .put("kind", e.getString("kind")).put("id", id).put("label", e.optString("label")))
    }

    fun dismiss(id: String, snooze: Boolean = false) {
        val e = entries.firstOrNull { it.getString("id") == id && it.optBoolean("ringing") } ?: return
        if (snooze && e.getString("kind") == "alarm") {
            // Separate one-shot keeps the repeating alarm's original clock time intact.
            val result = JSONObject(command(JSONObject().put("op", "set_alarm")
                .put("label", e.optString("label")).put("time_epoch_ms", System.currentTimeMillis() + 300_000)))
            if (result.optString("error").isNotEmpty()) return
        }
        e.put("ringing", false)
        if (!e.optBoolean("enabled")) {
            manager.cancel(pending(id)); entries.remove(e)
        }
        save()
        refreshRuntime()
    }

    private fun save() {
        check(prefs.edit().putString("entries", JSONArray(entries).toString()).commit()) { "Could not save schedule" }
    }

    private fun tickDue() {
        entries.toList().filter { it.optBoolean("enabled") }.forEach { fire(it.getString("id")) }
    }
    private val tick = object : Runnable {
        override fun run() {
            tickDue()
            onChanged?.invoke()
            handler.removeCallbacks(this)
            if (activeTimers) handler.postDelayed(this, 200)
        }
    }

    private fun refreshRuntime() {
        isRinging = ringing.isNotEmpty()
        if (activeTimers && !wake.isHeld) wake.acquire()
        if (!activeTimers && wake.isHeld) wake.release()
        handler.removeCallbacks(tick)
        if (activeTimers) handler.postDelayed(tick, 200)
        if (needsService && onServiceChanged == null) {
            try { context.startForegroundService(Intent(context, AlarmService::class.java)) }
            catch (e: Exception) { Log.e("EchoSchedule", "Cannot start alarm service", e) }
        }
        onServiceChanged?.invoke()
        onChanged?.invoke()
        onState?.invoke()
    }

    companion object {
        private var instance: AlarmScheduler? = null
        fun get(context: Context): AlarmScheduler = instance ?: AlarmScheduler(context.applicationContext).also { instance = it }
    }
}
