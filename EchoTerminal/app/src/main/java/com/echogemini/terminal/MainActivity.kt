package com.echogemini.terminal

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Toast
import org.json.JSONObject
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import kotlin.math.min

/**
 * The launcher. Boots straight into the ambient clock and stays there.
 *
 * ## Layering
 *
 * ```
 *   StatusOverlay   state + CAMERA ON indicator   (always on top)
 *   PushSurface     WebView, pushed content       (fades in and out)
 *   AmbientClock    local clock, no network       (always present)
 * ```
 *
 * The ordering matters twice. The clock is the base layer and is never removed,
 * so link state can never leave a blank screen. The camera indicator is above
 * the push surface, so no pushed HTML can cover up the fact that the camera is
 * streaming.
 *
 * ## What must survive the server being off
 *
 * Everything on this screen except pushed content. [Link] runs entirely on
 * background threads and reports in through callbacks; nothing here blocks on
 * it, and the clock ticks from a handler that does not know the link exists.
 * Verification step 12 tests this by deploying with the server stopped.
 */
class MainActivity : ComponentActivity(), Link.Listener, CameraSource.Callbacks {

    private lateinit var root: FrameLayout
    private lateinit var clock: AmbientClock
    private lateinit var push: PushSurface
    private lateinit var statusBar: StatusOverlay
    private lateinit var scheduler: AlarmScheduler
    private lateinit var scheduleScreen: ScheduleScreen
    private lateinit var alarmTile: Button
    private lateinit var timerTile: Button

    private var link: Link? = null
    private var capture: AudioCapture? = null
    private var playback: AudioPlayback? = null
    private var camera: CameraSource? = null

    private var sensorManager: SensorManager? = null
    private var lightSensor: Sensor? = null

    private var haveAudioPermission = false
    private var haveCameraPermission = false

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        haveAudioPermission = granted[Manifest.permission.RECORD_AUDIO] == true
        haveCameraPermission = granted[Manifest.permission.CAMERA] == true
        Log.i(TAG, "permissions: mic=$haveAudioPermission camera=$haveCameraPermission")
        startAudioIfPermitted()
    }

    // --- lifecycle ----------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // A wall clock that sleeps is not one. FLAG_KEEP_SCREEN_ON rather than a
        // wake lock: it is scoped to this window, so it cannot outlive the app.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
        window.addFlags(WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)

        buildUi()
        scheduler.onChanged = {
            scheduleScreen.render()
            updateTiles()
        }
        scheduler.onFired = { playback?.flush(); link?.sendControl(it.toString()) }
        scheduler.onState = { link?.sendControl(scheduler.snapshot()) }
        scheduler.restore()
        hideSystemBars()

        // As the HOME activity, back must not escape to a blank launcher.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                scheduleScreen.back()
                push.fadeToClock("back")
            }
        })

        sensorManager = getSystemService(SensorManager::class.java)
        lightSensor = sensorManager?.getDefaultSensor(Sensor.TYPE_LIGHT)
        if (lightSensor == null) {
            Log.i(TAG, "no light sensor; falling back to the clock-hour dim curve")
        }

        checkPermissions()
        startLink()
    }

    /**
     * The clock is built and shown before anything network- or hardware-related
     * is touched, and the order is not incidental.
     */
    private fun buildUi() {
        root = FrameLayout(this).apply { setBackgroundColor(android.graphics.Color.BLACK) }

        clock = AmbientClock(this)
        val home = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        home.addView(clock, LinearLayout.LayoutParams(-1, 0, 3f))
        val tiles = LinearLayout(this)
        scheduler = AlarmScheduler.get(this)
        scheduleScreen = ScheduleScreen(this, scheduler) { requestExactAlarms() }
        alarmTile = Button(this).apply {
            textSize = 25f; isAllCaps = false
            setOnClickListener { push.fadeToClock("alarms"); scheduleScreen.open("alarm") }
        }
        timerTile = Button(this).apply {
            textSize = 25f; isAllCaps = false
            setOnClickListener { push.fadeToClock("timers"); scheduleScreen.open("timer") }
        }
        tiles.addView(alarmTile, LinearLayout.LayoutParams(0, -1, 1f))
        tiles.addView(timerTile, LinearLayout.LayoutParams(0, -1, 1f))
        home.addView(tiles, LinearLayout.LayoutParams(-1, 0, 1f))
        root.addView(home, FrameLayout.LayoutParams(-1, -1))
        updateTiles()

        push = PushSurface(this)
        root.addView(
            push,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
            )
        )

        root.addView(scheduleScreen, FrameLayout.LayoutParams(-1, -1))
        statusBar = StatusOverlay(this)
        root.addView(
            statusBar,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                resources.displayMetrics.heightPixels / 10,
                Gravity.TOP
            )
        )

        setContentView(root)
        clock.dimFactor = AmbientClock.dimForHour()
    }

    override fun onResume() {
        super.onResume()
        clock.startTicking()
        scheduler.restore()
        scheduleScreen.render()
        hideSystemBars()
        lightSensor?.let {
            sensorManager?.registerListener(
                lightListener, it, SensorManager.SENSOR_DELAY_NORMAL
            )
        }
    }

    override fun onPause() {
        sensorManager?.unregisterListener(lightListener)
        super.onPause()
    }

    override fun onDestroy() {
        // Release the camera FIRST and unconditionally. Whatever else goes
        // wrong during teardown, a HAL1 camera left open is the failure that
        // needs a physical power cycle.
        camera?.shutdown()
        camera = null
        scheduler.onChanged = null
        scheduler.onFired = null
        scheduler.onState = null

        capture?.stop()
        playback?.stop()
        link?.stop()
        push.destroy()
        clock.stopTicking()
        super.onDestroy()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemBars()
    }

    @Suppress("DEPRECATION")
    private fun hideSystemBars() {
        // The deprecated flags are used deliberately: they work on API 30, and
        // WindowInsetsController's behaviour on this vendor's build has not been
        // verified on hardware.
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            )
    }

    // --- tap to talk --------------------------------------------------------

    /**
     * Tap anywhere. This is a permanent manual override, not a v1 shim (HANDOFF
     * section 1): the microphone is weak enough that the wake word will
     * sometimes miss, and there has to be a way to talk to the thing that does
     * not depend on it hearing you first.
     */
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.action != MotionEvent.ACTION_DOWN) return super.onTouchEvent(event)

        if (link?.state != Link.State.CONNECTED) {
            Log.i(TAG, "tap ignored: link is ${link?.state}")
            return true
        }
        Log.i(TAG, "tap to talk")
        link?.sendTap()
        return true
    }

    // --- permissions and capture --------------------------------------------

    private fun checkPermissions() {
        haveAudioPermission = ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        haveCameraPermission = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA
        ) == PackageManager.PERMISSION_GRANTED

        val missing = buildList {
            if (!haveAudioPermission) add(Manifest.permission.RECORD_AUDIO)
            if (!haveCameraPermission) add(Manifest.permission.CAMERA)
            if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(
                    this@MainActivity, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (missing.isEmpty()) {
            startAudioIfPermitted()
        } else {
            // On a kiosk device these are granted once during bring-up, or
            // pre-granted with `adb shell pm grant`. Asking is the fallback.
            permissionLauncher.launch(missing.toTypedArray())
        }
    }

    private fun startAudioIfPermitted() {
        if (!haveAudioPermission) {
            Log.w(TAG, "no RECORD_AUDIO; voice is unavailable but the clock is not")
            return
        }
        if (playback == null) {
            val player = AudioPlayback()
            player.onSpeakingChanged = { speaking ->
                // Half-duplex, enforced locally as well as by the server's mic
                // message -- whichever notices first wins.
                capture?.uplinkEnabled = !speaking
            }
            playback = if (player.start()) {
                player
            } else {
                Log.e(TAG, "AudioTrack would not start; no downlink audio")
                null
            }
        }
        if (capture == null) {
            val recorder = AudioCapture { pcm -> if (!scheduler.isRinging) link?.sendAudio(pcm) }
            capture = if (recorder.start()) {
                recorder
            } else {
                Log.e(TAG, "AudioRecord would not start; no uplink audio")
                null
            }
        }
    }

    // --- link ---------------------------------------------------------------

    private fun startLink() {
        if (BuildConfig.SHARED_SECRET.isEmpty()) {
            Log.e(
                TAG,
                "No shared secret compiled in. Set echo.serverUrl and echo.sharedSecret " +
                    "in EchoTerminal/local.properties and rebuild. The clock still works."
            )
        }
        link = Link(
            serverUrl = BuildConfig.SERVER_URL,
            sharedSecret = BuildConfig.SHARED_SECRET,
            deviceId = deviceId(),
            appVersion = BuildConfig.VERSION_NAME,
            listener = this
        ).also { it.start() }
    }

    @Suppress("HardwareIds")
    private fun deviceId(): String {
        // ANDROID_ID is stable across reboots and unique per device, and this is
        // a single-device deployment on a private tailnet -- it never leaves
        // the household.
        val androidId = try {
            Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID)
        } catch (e: Exception) {
            null
        }
        val suffix = androidId?.takeLast(8) ?: "unknown"
        return "${Build.DEVICE}-$suffix"
    }

    // --- Link.Listener ------------------------------------------------------

    override fun onLinkState(state: Link.State, detail: String) {
        Log.i(TAG, "link -> $state ($detail)")
        runOnUiThread {
            clock.indicator = when (state) {
                Link.State.CONNECTED -> AmbientClock.LinkIndicator.ONLINE
                Link.State.CONNECTING -> AmbientClock.LinkIndicator.CONNECTING
                Link.State.DISCONNECTED -> AmbientClock.LinkIndicator.OFFLINE
            }
            if (state != Link.State.CONNECTED) {
                // Link loss returns the display to the clock: pushed content is
                // by definition stale once the thing that pushed it is gone.
                push.fadeToClock("link lost")
                statusBar.state = Protocol.UiState.IDLE
                statusBar.cameraStreaming = false
                statusBar.shutterClosed = false
                capture?.uplinkEnabled = true
                playback?.flush()
                // And the camera goes with it. No server means no session.
                camera?.release()
            }
        }
    }

    override fun onControl(msg: Protocol.Incoming) {
        when (msg.type) {
            Protocol.Type.ALARM_COMMAND -> runOnUiThread {
                link?.sendControl(scheduler.command(msg.body))
            }
            Protocol.Type.WELCOME -> Log.i(
                TAG,
                "server welcome: live=${msg.bool("live_enabled")} " +
                    "audio=${msg.int("audio_up_rate")}->${msg.int("audio_down_rate")} Hz"
            )

            Protocol.Type.STATE -> runOnUiThread {
                statusBar.state = Protocol.UiState.from(msg.str("state"))
            }

            Protocol.Type.MIC -> {
                capture?.uplinkEnabled = msg.bool("enabled", true)
                if (msg.body.has("gain")) {
                    capture?.gain = msg.dbl("gain", 1.0).toFloat()
                }
            }

            Protocol.Type.INTERRUPT -> {
                // Barge-in. Do this off the UI thread's critical path: the
                // sooner the room goes quiet the better.
                playback?.flush()
                capture?.uplinkEnabled = true
            }

            Protocol.Type.DISPLAY -> {
                val cmd = Protocol.DisplayCommand.from(msg)
                if (cmd == null) {
                    link?.sendControl(
                        Protocol.error("bad_display", "could not parse the display command")
                    )
                } else {
                    Log.i(TAG, "display: ${PushSurface.describe(cmd)}")
                    runOnUiThread { push.show(cmd) }
                }
            }

            Protocol.Type.DISPLAY_CLEAR -> runOnUiThread { push.fadeToClock("cleared") }

            Protocol.Type.VIDEO -> handleVideoRequest(msg)

            Protocol.Type.ERROR -> Log.e(
                TAG, "server error ${msg.str("code")}: ${msg.str("message")}"
            )

            else -> Log.d(TAG, "ignoring ${msg.type}")
        }
    }

    override fun onAudioDown(pcm: ByteArray) {
        playback?.enqueue(pcm)
    }

    private fun updateTiles() {
        val state = JSONObject(scheduler.snapshot())
        alarmTile.text = "Alarms · ${state.getJSONArray("alarms").length()}"
        timerTile.text = "Timers · ${state.getJSONArray("timers").length()}"
    }

    private fun requestExactAlarms() {
        if (Build.VERSION.SDK_INT >= 31 && !scheduler.exactAllowed) {
            try { startActivity(Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                Uri.parse("package:$packageName"))) }
            catch (_: Exception) { Toast.makeText(this, "Open Android settings → Alarms & reminders", Toast.LENGTH_LONG).show() }
        }
    }

    // --- camera -------------------------------------------------------------

    /**
     * The ONLY place the camera is opened. Nothing here runs at boot, on tap, or
     * when a session starts -- only when the server explicitly asks for vision.
     */
    private fun handleVideoRequest(msg: Protocol.Incoming) {
        val enabled = msg.bool("enabled", false)

        if (!enabled) {
            camera?.release()
            runOnUiThread {
                statusBar.cameraStreaming = false
                statusBar.shutterClosed = false
            }
            return
        }

        if (!haveCameraPermission) {
            Log.w(TAG, "vision requested but CAMERA permission was not granted")
            link?.sendCameraStatus(
                Protocol.CameraStatus.ERROR, "the app does not hold CAMERA permission"
            )
            return
        }

        val source = camera ?: CameraSource(this).also { camera = it }
        val fps = min(msg.dbl("fps", Protocol.VIDEO_MAX_FPS), Protocol.VIDEO_MAX_FPS)
        source.open(fps, msg.int("jpeg_quality", 80))
    }

    // --- CameraSource.Callbacks ---------------------------------------------

    override fun onFrame(jpeg: ByteArray) {
        link?.sendVideo(jpeg)
    }

    override fun onStatus(status: Protocol.CameraStatus, detail: String) {
        Log.i(TAG, "camera status: ${status.wire} $detail")
        link?.sendCameraStatus(status, detail)
        runOnUiThread {
            statusBar.cameraStreaming = status == Protocol.CameraStatus.STREAMING
            statusBar.shutterClosed = status == Protocol.CameraStatus.SHUTTER_CLOSED
        }
    }

    // --- ambient dimming ----------------------------------------------------

    private val lightListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val lux = event.values.firstOrNull() ?: return
            // A clock that blazes at 3 a.m. is its own problem (HANDOFF 8.1).
            // The panel is LCD, so this is about the room, not burn-in.
            val target = when {
                lux < 2f -> 0.22f
                lux < 10f -> 0.40f
                lux < 50f -> 0.65f
                lux < 200f -> 0.85f
                else -> 1.0f
            }
            // Take the dimmer of sensor and clock-hour, so a lamp left on at
            // 3 a.m. does not put the display back to full brightness.
            clock.dimFactor = min(target, AmbientClock.dimForHour())
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
    }

    private companion object {
        const val TAG = "EchoTerminal"
    }
}
