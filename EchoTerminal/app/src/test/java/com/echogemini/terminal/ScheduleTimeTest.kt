package com.echogemini.terminal

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.ZonedDateTime
import java.time.ZoneId

class ScheduleTimeTest {
    private val zone = ZoneId.of("America/New_York")
    private fun at(value: String) = ZonedDateTime.parse(value).toInstant().toEpochMilli()

    @Test fun repeatSkipsMissedDaysAfterPowerLoss() {
        assertEquals(at("2026-09-11T07:00-04:00[America/New_York]"), ScheduleTime.nextRepeat(
            at("2026-09-02T07:00-04:00[America/New_York]"), listOf("mon", "wed", "fri"),
            at("2026-09-09T09:00-04:00[America/New_York]"), zone))
    }
    @Test fun nextOccurrenceCanBeToday() {
        assertEquals(at("2026-09-09T17:00-04:00[America/New_York]"), ScheduleTime.nextRepeat(
            at("2026-09-02T17:00-04:00[America/New_York]"), listOf("wed"),
            at("2026-09-09T09:00-04:00[America/New_York]"), zone))
    }
    @Test fun repeatKeepsLocalTimeAcrossDaylightSaving() {
        assertEquals(at("2026-11-01T07:00-05:00[America/New_York]"), ScheduleTime.nextRepeat(
            at("2026-10-31T07:00-04:00[America/New_York]"), ScheduleTime.days,
            at("2026-10-31T07:00-04:00[America/New_York]"), zone))
    }
    @Test fun nonexistentSpringTimeMovesForwardByGap() {
        assertEquals(at("2026-03-08T03:30-04:00[America/New_York]"), ScheduleTime.nextRepeat(
            at("2026-03-07T02:30-05:00[America/New_York]"), listOf("sun"),
            at("2026-03-07T09:00-05:00[America/New_York]"), zone))
    }
    @Test fun timerSurvivesProcessDeathWithoutWallClockDrift() {
        assertEquals(5000L, ScheduleTime.remaining(15000, 999999, 3, 3, 10000, 500000))
    }
    @Test fun timerUsesEpochAfterReboot() {
        assertEquals(5000L, ScheduleTime.remaining(15000, 505000, 3, 4, 100, 500000))
    }
    @Test fun overdueTimerFiresImmediately() {
        assertEquals(0L, ScheduleTime.remaining(15000, 505000, 3, 4, 100, 600000))
    }
}
