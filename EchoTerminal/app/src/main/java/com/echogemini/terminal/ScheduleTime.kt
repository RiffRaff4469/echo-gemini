package com.echogemini.terminal

import java.time.Instant
import java.time.ZoneId

/** Pure calendar math: weekday names match protocol.py, Monday first. */
object ScheduleTime {
    val days = listOf("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    fun nextRepeat(previous: Long, selected: List<String>, now: Long,
                   zone: ZoneId = ZoneId.systemDefault()): Long {
        val time = Instant.ofEpochMilli(previous).atZone(zone).toLocalTime()
        return nextRepeatAt(time.hour, time.minute, selected, now, zone)
    }

    fun nextRepeatAt(hour: Int, minute: Int, selected: List<String>, now: Long,
                     zone: ZoneId = ZoneId.systemDefault()): Long {
        require(selected.isNotEmpty() && selected.all { it in days })
        val time = java.time.LocalTime.of(hour, minute)
        val today = Instant.ofEpochMilli(now).atZone(zone).toLocalDate()
        for (offset in 0L..7L) {
            val date = today.plusDays(offset)
            val candidate = date.atTime(time).atZone(zone).toInstant().toEpochMilli()
            if (days[date.dayOfWeek.value - 1] in selected && candidate > now) return candidate
        }
        error("No next weekday")
    }

    fun remaining(deadlineElapsed: Long, deadlineEpoch: Long, savedBoot: Int,
                  boot: Int, elapsed: Long, epoch: Long): Long =
        ((if (savedBoot == boot) deadlineElapsed - elapsed else deadlineEpoch - epoch))
            .coerceAtLeast(0)
}
