package com.echogemini.terminal

import android.content.Context
import android.app.AlarmManager
import android.os.SystemClock
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowAlarmManager
import org.robolectric.shadows.ShadowPowerManager
import org.robolectric.annotation.Config
import org.robolectric.annotation.LooperMode

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30])
@LooperMode(LooperMode.Mode.PAUSED)
class AlarmSchedulerTest {
    private lateinit var context: Context
    private lateinit var scheduler: AlarmScheduler
    private fun clearSingleton() {
        AlarmScheduler::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, null)
    }
    @Before fun setup() {
        clearSingleton()
        context = RuntimeEnvironment.getApplication()
        scheduler = AlarmScheduler.get(context)
        scheduler.onServiceChanged = {} // Exercise scheduling without starting actual audio.
    }
    @After fun cleanup() {
        val state = JSONObject(scheduler.snapshot())
        listOf("alarms", "timers").forEach { kind ->
            val entries = state.getJSONArray(kind)
            for (i in 0 until entries.length()) command("cancel", "id" to entries.getJSONObject(i).getString("id"))
        }
        clearSingleton()
    }
    private fun command(op: String, vararg values: Pair<String, Any>): JSONObject {
        val cmd = JSONObject().put("op", op).put("req_id", "request-1")
        values.forEach { (key, value) -> cmd.put(key, value) }
        return JSONObject(scheduler.command(cmd))
    }

    @Test fun timerAckIsPersistedAndCorrelated() {
        val state = command("set_timer", "duration_s" to 60, "label" to "Tea", "id" to "t1")
        assertEquals("request-1", state.getString("req_id"))
        assertEquals("", state.getString("error"))
        assertEquals("Tea", state.getJSONArray("timers").getJSONObject(0).getString("label"))
        val stored = context.createDeviceProtectedStorageContext().getSharedPreferences("schedule", 0)
        assertTrue(stored.getString("entries", "")!!.contains("Tea"))
        assertTrue(scheduler.activeTimers)
        assertTrue(ShadowPowerManager.getLatestWakeLock().isHeld)
        val scheduled = requireNotNull(shadowOf(context.getSystemService(AlarmManager::class.java)).peekNextScheduledAlarm())
        assertEquals(AlarmManager.ELAPSED_REALTIME_WAKEUP, scheduled.type)
        assertTrue(scheduled.allowWhileIdle)
        assertEquals(ShadowAlarmManager.WINDOW_EXACT, scheduled.windowLengthMs)
    }
    @Test fun timerFiresOnceAndDismissReleasesRuntime() {
        command("set_timer", "duration_s" to 1, "id" to "t1")
        var fired = 0
        scheduler.onFired = { fired++ }
        SystemClock.sleep(1500)
        scheduler.fire("t1")
        scheduler.fire("t1")
        assertEquals(1, fired)
        assertTrue(scheduler.isRinging)
        assertFalse(scheduler.activeTimers)
        assertFalse(ShadowPowerManager.getLatestWakeLock().isHeld)
        scheduler.dismiss("t1")
        assertFalse(scheduler.needsService)
        assertEquals(0, JSONObject(scheduler.snapshot()).getJSONArray("timers").length())
    }
    @Test fun ambiguousCancelDoesNotDeleteAnything() {
        command("set_timer", "duration_s" to 60, "label" to "Tea")
        command("set_timer", "duration_s" to 60, "label" to "Tea")
        val state = command("cancel", "label" to "Tea")
        assertTrue(state.getString("error").isNotEmpty())
        assertEquals(2, state.getJSONArray("timers").length())
    }
    @Test fun snoozePreservesRepeatTimeAndQueuesOneShot() {
        val now = System.currentTimeMillis()
        command("set_alarm", "id" to "a", "time_epoch_ms" to now + 60000,
            "days" to org.json.JSONArray(ScheduleTime.days))
        // Simulate a persisted occurrence that became overdue while the process
        // was absent. Robolectric's elapsed clock does not advance JVM wall time.
        val prefs = context.createDeviceProtectedStorageContext().getSharedPreferences("schedule", 0)
        val saved = org.json.JSONArray(prefs.getString("entries", "[]"))
        saved.getJSONObject(0).put("time_epoch_ms", now - 1000)
        prefs.edit().putString("entries", saved.toString()).commit()
        clearSingleton()
        scheduler = AlarmScheduler.get(context)
        scheduler.onServiceChanged = {}
        scheduler.fire("a")
        assertTrue(scheduler.isRinging)
        val next = JSONObject(scheduler.snapshot()).getJSONArray("alarms").getJSONObject(0).getLong("time_epoch_ms")
        scheduler.dismiss("a", true)
        val alarms = JSONObject(scheduler.snapshot()).getJSONArray("alarms")
        assertEquals(2, alarms.length())
        assertEquals(next, alarms.getJSONObject(0).getLong("time_epoch_ms"))
        assertEquals(0, alarms.getJSONObject(1).getJSONArray("days").length())
        assertEquals((System.currentTimeMillis() + 300000).toDouble(), alarms.getJSONObject(1).getLong("time_epoch_ms").toDouble(), 100.0)
        assertFalse(scheduler.isRinging)
    }
    @Test fun restoreReadsPersistedScheduleWithoutServer() {
        command("set_alarm", "id" to "a", "time_epoch_ms" to System.currentTimeMillis() + 60000)
        clearSingleton()
        scheduler = AlarmScheduler.get(context)
        scheduler.onServiceChanged = {}
        scheduler.restore()
        assertEquals("a", JSONObject(scheduler.snapshot()).getJSONArray("alarms").getJSONObject(0).getString("id"))
    }
    @Test fun invalidTimerDoesNotPersist() {
        val state = command("set_timer", "duration_s" to -1)
        assertTrue(state.getString("error").isNotEmpty())
        assertEquals(0, state.getJSONArray("timers").length())
    }

    @Test fun timerRecoversRemainingTimeAfterProcessRecreation() {
        command("set_timer", "id" to "t", "duration_s" to 60)
        val prefs = context.createDeviceProtectedStorageContext().getSharedPreferences("schedule", 0)
        val saved = prefs.getString("entries", "[]")
        command("cancel", "id" to "t") // Release the old runtime as process death would.
        prefs.edit().putString("entries", saved).commit()
        SystemClock.sleep(1000)
        clearSingleton()
        scheduler = AlarmScheduler.get(context)
        scheduler.onServiceChanged = {}
        scheduler.restore()
        val remaining = JSONObject(scheduler.snapshot()).getJSONArray("timers").getJSONObject(0).getDouble("remaining_s")
        assertEquals(59.0, remaining, 0.1)
        assertTrue(scheduler.activeTimers)
    }

    @Test @Config(sdk = [31]) fun permissionDenialSchedulesFallbackAndGrantReschedulesExact() {
        ShadowAlarmManager.setCanScheduleExactAlarms(false)
        val state = command("set_alarm", "id" to "a", "time_epoch_ms" to System.currentTimeMillis() + 60000)
        assertEquals("", state.getString("error"))
        assertFalse(state.getBoolean("exact_allowed"))
        val manager = shadowOf(context.getSystemService(AlarmManager::class.java))
        assertTrue(requireNotNull(manager.peekNextScheduledAlarm()).allowWhileIdle)
        assertNotEquals(ShadowAlarmManager.WINDOW_EXACT, requireNotNull(manager.peekNextScheduledAlarm()).windowLengthMs)
        ShadowAlarmManager.setCanScheduleExactAlarms(true)
        scheduler.restore()
        assertTrue(scheduler.exactAllowed)
        assertEquals(ShadowAlarmManager.WINDOW_EXACT, requireNotNull(manager.peekNextScheduledAlarm()).windowLengthMs)
    }
}
