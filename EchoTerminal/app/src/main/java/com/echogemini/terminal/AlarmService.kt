package com.echogemini.terminal

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.IBinder
import kotlin.math.PI
import kotlin.math.sin

/** Survives activity recreation; only runs while a countdown or ringing needs it. */
class AlarmService : Service() {
    private lateinit var scheduler: AlarmScheduler
    private var chime: AudioTrack? = null
    private var showingRing = false

    override fun onCreate() {
        super.onCreate()
        val notifications = getSystemService(NotificationManager::class.java)
        notifications.createNotificationChannel(NotificationChannel(CHANNEL, "Alarms and timers",
            NotificationManager.IMPORTANCE_HIGH).apply { setSound(null, null) })
        startForeground(17, notification(false))
        scheduler = AlarmScheduler.get(this)
        scheduler.onServiceChanged = { update() }
        scheduler.restore()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        update()
        return START_STICKY
    }

    private fun notification(ringing: Boolean): Notification {
        val launch = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(if (ringing) "Alarm / Timer done" else "Timer running")
            .setContentText(if (ringing) "Tap to snooze or dismiss" else "Countdown works offline")
            .setContentIntent(launch).setOngoing(true).setCategory(Notification.CATEGORY_ALARM)
            .apply { if (ringing) setFullScreenIntent(launch, true) }.build()
    }

    private fun update() {
        val ring = scheduler.ringing.isNotEmpty()
        if (ring && chime == null) startChime()
        if (!ring) stopChime()
        if (ring != showingRing) {
            getSystemService(NotificationManager::class.java).notify(17, notification(ring))
            showingRing = ring
        }
        if (!scheduler.needsService) stopSelf()
    }

    private fun startChime() {
        // One static two-second buffer; native looping avoids a busy audio thread.
        val rate = 24000
        val samples = ShortArray(rate * 2) { i ->
            val t = i.toDouble() / rate
            val start = if (t < 0.55) 0.0 else 0.65
            val within = t - start
            val audible = (t < 0.55 || t in 0.65..1.2)
            val envelope = if (audible) (within / 0.02).coerceIn(0.0, 1.0) *
                ((0.55 - within) / 0.08).coerceIn(0.0, 1.0) else 0.0
            (sin(2 * PI * (if (start == 0.0) 660 else 880) * t) * envelope * 26000).toInt().toShort()
        }
        val track = AudioTrack.Builder().setAudioAttributes(AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ALARM).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(rate)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(AudioTrack.MODE_STATIC).setBufferSizeInBytes(samples.size * 2).build()
        try {
            track.write(samples, 0, samples.size, AudioTrack.WRITE_BLOCKING)
            track.setLoopPoints(0, samples.size, -1)
            track.setVolume(1f)
            track.play()
            chime = track
        } catch (e: Exception) { track.release(); android.util.Log.e("EchoChime", "Audio failed", e) }
    }

    private fun stopChime() { chime?.let { it.stop(); it.release() }; chime = null }
    override fun onDestroy() {
        scheduler.onServiceChanged = null
        stopChime()
        super.onDestroy()
    }
    override fun onBind(intent: Intent?): IBinder? = null
    companion object { private const val CHANNEL = "local_alarms" }
}

class AlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val scheduler = AlarmScheduler.get(context)
        if (intent.action == "com.echogemini.ALARM") {
            scheduler.fire(intent.data?.lastPathSegment ?: return)
        } else scheduler.restore()
    }
}
