package com.echogemini.terminal

import android.app.AlertDialog
import android.app.TimePickerDialog
import android.content.Context
import android.graphics.Color
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.*
import org.json.JSONArray
import org.json.JSONObject
import java.text.DateFormat
import java.util.Calendar
import java.util.Date
import kotlin.math.ceil

/** Large touch targets, local add/cancel controls, and a ring screen above pushed cards. */
class ScheduleScreen(context: Context, private val scheduler: AlarmScheduler,
                     private val requestExact: () -> Unit) : LinearLayout(context) {
    var page = "home"
        private set
    private var signature = ""
    private val countdowns = mutableListOf<Pair<TextView, String>>()
    private val pad = (12 * resources.displayMetrics.density).toInt()

    init { orientation = VERTICAL; setPadding(pad, pad * 3, pad, pad); visibility = GONE }

    fun open(kind: String) { page = kind; signature = ""; render() }
    fun back() { if (scheduler.ringing.isEmpty()) open("home") }

    fun render() {
        val snapshot = JSONObject(scheduler.snapshot())
        val ring = scheduler.ringing.firstOrNull()
        val newSignature = "$page|${snapshot.getJSONArray("alarms")}|" +
            snapshot.getJSONArray("timers").let { a ->
                (0 until a.length()).joinToString { a.getJSONObject(it).let { "${it.optString("id")}:${it.optBoolean("ringing")}" } }
            } + "|${scheduler.exactAllowed}|${ring?.optString("id")}"
        if (newSignature == signature) {
            val timers = snapshot.getJSONArray("timers")
            countdowns.forEach { (view, id) ->
                for (i in 0 until timers.length()) if (timers.getJSONObject(i).getString("id") == id)
                    view.text = describe(timers.getJSONObject(i))
            }
            return
        }
        signature = newSignature
        removeAllViews()
        countdowns.clear()
        visibility = if (page == "home" && ring == null) GONE else VISIBLE
        if (visibility == GONE) return
        setBackgroundColor(Color.rgb(9, 14, 22))
        if (ring != null) {
            val timer = ring.getString("kind") == "timer"
            addView(text(if (timer) "Timer done" else "Alarm", 38f))
            addView(text(ring.optString("label").ifEmpty { if (timer) "Time is up" else "Good morning" }, 30f),
                LayoutParams(-1, 0, 1f))
            val buttons = LinearLayout(context)
            if (!timer) buttons.addView(button("Snooze · 5 min") { scheduler.dismiss(ring.getString("id"), true) }, LayoutParams(0, -1, 1f))
            buttons.addView(button("Dismiss") { scheduler.dismiss(ring.getString("id")) }, LayoutParams(0, -1, 1f))
            addView(buttons, LayoutParams(-1, pad * 7))
            return
        }
        val header = LinearLayout(context)
        header.addView(button("‹ Clock") { back() }, LayoutParams(0, -2, 1f))
        header.addView(text(if (page == "alarm") "Alarms" else "Timers", 30f), LayoutParams(0, -1, 2f))
        header.addView(button("+ Add") { if (page == "alarm") addAlarm() else addTimer() }, LayoutParams(0, -2, 1f))
        addView(header)
        if (!scheduler.exactAllowed) addView(button("Allow exact alarms · timing may be delayed") { requestExact() })
        val list = LinearLayout(context).apply { orientation = VERTICAL }
        val entries = snapshot.getJSONArray(if (page == "alarm") "alarms" else "timers")
        if (entries.length() == 0) list.addView(text(if (page == "alarm") "No alarms set" else "No timers running", 30f))
        for (i in 0 until entries.length()) {
            val e = entries.getJSONObject(i)
            val row = LinearLayout(context).apply { setPadding(0, pad, 0, pad) }
            val description = text(describe(e), 25f)
            row.addView(description, LayoutParams(0, -2, 3f))
            if (page == "timer") countdowns.add(description to e.getString("id"))
            row.addView(button("Cancel") { apply(JSONObject().put("op", "cancel").put("id", e.getString("id"))) }, LayoutParams(0, -2, 1f))
            list.addView(row)
        }
        addView(ScrollView(context).apply { addView(list) }, LayoutParams(-1, 0, 1f))
    }

    private fun describe(e: JSONObject): String {
        val label = e.optString("label").ifEmpty { if (e.getString("kind") == "timer") "Timer" else "Alarm" }
        if (e.getString("kind") == "timer") {
            val s = ceil(e.optDouble("remaining_s")).toLong()
            return "$label  ${s / 3600}:${(s / 60 % 60).toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}"
        }
        val days = e.optJSONArray("days") ?: JSONArray()
        val repeat = if (days.length() == 0) DateFormat.getDateInstance(DateFormat.SHORT).format(Date(e.getLong("time_epoch_ms")))
            else (0 until days.length()).joinToString(" ") { days.getString(it) }
        return "$label  ${DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(e.getLong("time_epoch_ms")))}\n$repeat"
    }

    private fun addAlarm() {
        val now = Calendar.getInstance()
        TimePickerDialog(context, { _, hour, minute ->
            val at = Calendar.getInstance().apply { set(Calendar.HOUR_OF_DAY, hour); set(Calendar.MINUTE, minute); set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0) }
            if (at.timeInMillis <= System.currentTimeMillis()) at.add(Calendar.DAY_OF_YEAR, 1)
            val selected = BooleanArray(7)
            AlertDialog.Builder(context).setTitle("Repeat days · none = once")
                .setMultiChoiceItems(ScheduleTime.days.toTypedArray(), selected) { _, which, checked -> selected[which] = checked }
                .setNegativeButton("Cancel", null).setPositiveButton("Next") { _, _ ->
                    val label = input("Alarm label")
                    AlertDialog.Builder(context).setTitle("Name the alarm").setView(label)
                        .setNegativeButton("Cancel", null).setPositiveButton("Set alarm") { _, _ ->
                            apply(JSONObject().put("op", "set_alarm").put("time_epoch_ms", at.timeInMillis)
                                .put("days", JSONArray(ScheduleTime.days.filterIndexed { i, _ -> selected[i] }))
                                .put("label", label.text.toString()))
                        }.show()
                }.show()
        }, now.get(Calendar.HOUR_OF_DAY), now.get(Calendar.MINUTE), false).show()
    }

    private fun addTimer() {
        val fields = LinearLayout(context).apply { orientation = VERTICAL }
        val minutes = input("Minutes (up to 1440)").apply { inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL }
        val label = input("Timer label")
        fields.addView(minutes); fields.addView(label)
        AlertDialog.Builder(context).setTitle("Start timer").setView(fields)
            .setNegativeButton("Cancel", null).setPositiveButton("Start") { _, _ ->
                val duration = (minutes.text.toString().toDoubleOrNull() ?: 0.0) * 60
                if (!duration.isFinite()) Toast.makeText(context, "Enter a valid duration", Toast.LENGTH_LONG).show()
                else apply(JSONObject().put("op", "set_timer").put("duration_s", duration).put("label", label.text.toString()))
            }.show()
    }

    private fun apply(cmd: JSONObject) {
        val error = JSONObject(scheduler.command(cmd)).optString("error")
        if (error.isNotEmpty()) Toast.makeText(context, error, Toast.LENGTH_LONG).show()
        render()
    }
    private fun input(hintText: String) = EditText(context).apply { hint = hintText; setSingleLine(); textSize = 24f }
    private fun text(value: String, size: Float) = TextView(context).apply {
        text = value; textSize = size; setTextColor(Color.WHITE); gravity = Gravity.CENTER; setPadding(pad, pad, pad, pad)
    }
    private fun button(label: String, action: () -> Unit) = Button(context).apply {
        text = label; textSize = 22f; isAllCaps = false; minHeight = pad * 5; setOnClickListener { action() }
    }
}
