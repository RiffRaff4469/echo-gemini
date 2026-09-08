package com.echogemini.terminal

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.graphics.drawable.GradientDrawable
import android.hardware.SensorManager
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import java.util.concurrent.atomic.AtomicLong
import android.widget.Toast
import org.json.JSONObject
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import kotlin.math.max
import kotlin.math.min

/**
 * The launcher. Boots straight into the ambient display and stays there.
 *
 * ## Layering
 *
 * ```
 *   StatusOverlay   CAMERA ON indicator            (always on top)
 *   ScheduleScreen  alarms and timers, native
 *   AmbientWeb      the ported ambient UI          (UI-BRIEF-9, preferred)
 *   canvasLayer     AmbientClock + tiles + push    (UI-BRIEF-4, the fallback)
 * ```
 *
 * The ordering matters twice. There is always an ambient layer under
 * everything, so link state can never leave a blank screen. The camera
 * indicator is above every surface that can render server content, so nothing
 * pushed -- into the WebView or the Canvas -- can cover up the fact that the
 * camera is streaming.
 *
 * ## Two ambients, one at a time
 *
 * [AmbientWeb] is the display. The Canvas ambient stays compiled in and takes
 * over whenever the WebView cannot: the custom ROM has the system WebView
 * removed, so "there is no WebView" is a real configuration, not a
 * theoretical one. Both are built here; exactly one is visible, and the
 * switch is one-way per boot (see [enterCanvasMode]).
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
    private lateinit var canvasLayer: FrameLayout
    private lateinit var clock: AmbientClock
    private lateinit var statusBar: StatusOverlay
    private lateinit var scheduler: AlarmScheduler
    private lateinit var stopwatch: Stopwatch
    private lateinit var scheduleScreen: ScheduleScreen
    private lateinit var alarmTile: Button
    private lateinit var timerTile: Button
    private lateinit var stopwatchTile: Button
    /** Last chip text pushed to the WebView (seconds-resolution throttle). */
    private var lastStopwatchChip = ""
    private var lastStopwatchRunning = false

    /** Main-thread handler for the button hold timer. */
    private val handler = Handler(Looper.getMainLooper())

    /** Device-owned mute persistence (HARDWARE-BRIEF-7 v2). */
    private val prefs by lazy { getSharedPreferences("echo", MODE_PRIVATE) }

    /** The mute LED once the discovery spike finds a node; null = chip only. */
    private var led: MicLed? = null

    /** Interface for the mute LED (work item 5); chip-only until implemented. */
    interface MicLed {
        fun setMuted(on: Boolean)
    }

    /** The WebView ambient, or null once (or if ever) it failed. */
    private var web: AmbientWeb? = null

    /** Only exists in Canvas mode; the WebView ambient renders its own cards. */
    private var push: PushSurface? = null

    private var link: Link? = null
    private var capture: AudioCapture? = null
    private var playback: AudioPlayback? = null

    /**
     * Music, on its own AudioTrack (protocol v1.5). Started alongside
     * [playback] but never in place of it: see [Protocol.Channel.AUDIO_MUSIC].
     */
    private var music: MusicPlayback? = null
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
        stopwatch.onChanged = {
            // Live ticks update the stopwatch page in place; tiles refresh
            // cheaply. Full renders only happen on page opens/transitions.
            scheduleScreen.stopwatchTick()
            updateTiles()
        }
        stopwatch.onState = { link?.sendControl(it) }
        hideSystemBars()

        // As the HOME activity, back must not escape to a blank launcher.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                scheduleScreen.back()
                push?.fadeToClock("back")
                web?.displayClear()
            }
        })

        sensorManager = getSystemService(SensorManager::class.java)
        lightSensor = sensorManager?.getDefaultSensor(Sensor.TYPE_LIGHT)
        if (lightSensor == null) {
            Log.i(TAG, "no light sensor; falling back to the clock-hour dim curve")
        }

        startMusicPlayback()
        checkPermissions()
        startLink()

        // Privacy mute is device-owned and persists across reboots (v2). The
        // persisted state must be live before the link connects so the hello
        // caps announce it, and before the mic starts so a muted boot never
        // sends audio for even a moment.
        applyPrivacyMute(prefs.getBoolean(PREF_MUTED, false))
    }

    /**
     * Music output needs no runtime permission, so it is started here rather
     * than in [startAudioIfPermitted] -- a device where the microphone was
     * denied is still perfectly good as a speaker, and quietly losing Spotify
     * along with the voice path would be a confusing way to find that out.
     */
    private fun startMusicPlayback() {
        val player = MusicPlayback()
        music = if (player.start()) {
            player
        } else {
            Log.e(TAG, "music AudioTrack would not start; Spotify audio is unavailable")
            null
        }
    }

    /**
     * The ambient layer is built and shown before anything network- or
     * hardware-related is touched, and the order is not incidental.
     *
     * The Canvas fallback is assembled first and in full, then hidden if the
     * WebView starts. Building it either way costs a handful of views and no
     * ticking timer, and it means a WebView that dies an hour from now is a
     * `visibility` change rather than a screen that has to be constructed
     * while it is already blank.
     */
    private fun buildUi() {
        root = FrameLayout(this).apply { setBackgroundColor(android.graphics.Color.BLACK) }

        canvasLayer = FrameLayout(this)
        root.addView(canvasLayer, FrameLayout.LayoutParams(-1, -1))

        clock = AmbientClock(this)
        canvasLayer.addView(clock, FrameLayout.LayoutParams(-1, -1))

        scheduler = AlarmScheduler.get(this)
        stopwatch = Stopwatch.get(this)
        scheduleScreen = ScheduleScreen(this, scheduler, stopwatch) { requestExactAlarms() }

        // Proportions match what AmbientClock reserves for its own bottom rail,
        // so the chips and the weather card sit on one line.
        val panelHeight = resources.displayMetrics.heightPixels
        val tileHeight = (panelHeight * 0.19f).toInt()
        alarmTile = ambientTile(tileHeight) { openSchedule("alarm") }
        timerTile = ambientTile(tileHeight) { openSchedule("timer") }
        stopwatchTile = ambientTile(tileHeight) { openSchedule("stopwatch") }
        val tiles = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        tiles.addView(alarmTile, LinearLayout.LayoutParams(-2, -1))
        tiles.addView(
            timerTile,
            LinearLayout.LayoutParams(-2, -1).apply {
                marginStart = (tileHeight * 0.16f).toInt()
            }
        )
        tiles.addView(
            stopwatchTile,
            LinearLayout.LayoutParams(-2, -1).apply {
                marginStart = (tileHeight * 0.16f).toInt()
            }
        )
        canvasLayer.addView(
            tiles,
            FrameLayout.LayoutParams(-2, tileHeight, Gravity.BOTTOM or Gravity.END).apply {
                bottomMargin = (panelHeight * 0.06f).toInt()
                marginEnd = (resources.displayMetrics.widthPixels * 0.035f).toInt()
            }
        )

        // The ported ambient UI, if this device can host one. Added above the
        // Canvas layer rather than instead of it, so the fallback is a
        // visibility flip.
        web = AmbientWeb.createOrNull(this, webCallbacks)
        web?.let { root.addView(it, FrameLayout.LayoutParams(-1, -1)) }

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

        if (web == null) {
            enterCanvasMode("this device has no usable WebView")
        } else {
            // The page draws its own conversation chrome; the native band
            // keeps the one thing it must never share -- the camera pill.
            canvasLayer.visibility = View.GONE
            statusBar.showConversation = false
        }
        updateTiles()

        setContentView(root)
        clock.dimFactor = AmbientClock.dimForHour()
    }

    /**
     * Hand the screen back to the Canvas ambient (UI-BRIEF-4) and never ask
     * the WebView again this boot.
     *
     * One-way on purpose: the two reasons this is reached -- no WebView
     * installed, and a renderer that died -- are both things that retrying
     * would only rediscover, and a display that flickers between two designs
     * is worse than one that settles on the plainer of them.
     */
    private fun enterCanvasMode(reason: String) {
        if (push != null) return
        Log.w(TAG, "ambient falls back to the Canvas clock: $reason")

        web?.let { dead ->
            web = null
            // Never destroy a WebView from inside its own client callback.
            root.post { root.removeView(dead); dead.destroy() }
        }

        push = PushSurface(this).also {
            canvasLayer.addView(it, FrameLayout.LayoutParams(-1, -1))
        }
        canvasLayer.visibility = View.VISIBLE
        statusBar.showConversation = true
        clock.dimFactor = AmbientClock.dimForHour()
        clock.startTicking()
        updateTiles()
    }

    /**
     * A translucent chip rather than a Material button: the default button
     * background is an opaque grey rectangle with an elevation shadow, which on
     * an ambient gradient reads as a bug. Same click behaviour and same label
     * as before -- only the skin changed.
     */
    private fun ambientTile(height: Int, onClick: () -> Unit): Button = Button(this).apply {
        isAllCaps = false
        setTextColor(android.graphics.Color.argb(232, 226, 233, 244))
        setTextSize(TypedValue.COMPLEX_UNIT_PX, height * 0.26f)
        background = GradientDrawable().apply {
            cornerRadius = height * 0.22f
            setColor(android.graphics.Color.argb(20, 255, 255, 255))
            setStroke(
                max(1, (resources.displayMetrics.heightPixels * 0.0025f).toInt()),
                android.graphics.Color.argb(38, 255, 255, 255)
            )
        }
        val pad = (height * 0.34f).toInt()
        setPadding(pad, 0, pad, 0)
        elevation = 0f
        stateListAnimator = null
        setOnClickListener { onClick() }
    }

    override fun onResume() {
        super.onResume()
        // Only in Canvas mode: ticking a GONE clock is an invalidate a second
        // for a view nobody can see.
        if (push != null) clock.startTicking()
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
        music?.stop()
        link?.stop()
        push?.destroy()
        web?.destroy()
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

    // --- screen taps are UI-only (v1.6) ------------------------------------

    /**
     * v1.6 (HARDWARE-BRIEF-7 v2): screen taps no longer start or stop
     * conversations -- the screen is for UI only (widgets, chips, pages).
     * The wake word and the physical mic button are the session triggers.
     * This hook stays so the Canvas fallback surface consumes the touch, and
     * does nothing chat-related.
     */
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.action != MotionEvent.ACTION_DOWN) return super.onTouchEvent(event)
        Log.i(TAG, "tap ignored: screen taps are UI-only since v1.6")
        return true
    }

    /**
     * The WebView page's document click handler routes here via
     * `EchoNative.tap()`. Same rule as [onTouchEvent]: UI-only, no chat.
     */
    private fun handleTap() {
        Log.i(TAG, "tap ignored: screen taps are UI-only since v1.6")
    }

    // --- the physical mic button (HARDWARE-BRIEF-7 v2) ----------------------

    /**
     * The mic button arrives as KEYCODE_F1 (131) once the device-side
     * keylayout (`gating.kl`: key 116 -> F1) stops the stock LOS behaviour of
     * treating it as the power key. MICMUTE (277) is not exposed below API 35,
     * and system keys such as POWER or ASSIST are intercepted by the framework
     * before the app ever sees them -- F1 is a plain pass-through keycode.
     */
    private val micButtonDownAt = AtomicLong(0L)

    private val holdTask = object : Runnable {
        override fun run() {
            if (micButtonDownAt.get() == 0L) return  // released before the hold
            Log.i(TAG, "mic button: HOLD -> mute toggle")
            togglePrivacyMute()
        }
    }

    private fun keycodeIsMicButton(keyCode: Int): Boolean =
        keyCode == KeyEvent.KEYCODE_F1 || keyCode == 131

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        // Debug aid while the keylayout lands: log any key that reaches the
        // activity so a physical press tells us the framework's actual code.
        if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) {
            Log.i(TAG, "key down: ${event.keyCode} (${KeyEvent.keyCodeToString(event.keyCode)})")
        }
        if (!keycodeIsMicButton(event.keyCode)) return super.dispatchKeyEvent(event)
        when (event.action) {
            KeyEvent.ACTION_DOWN -> {
                // Ignore framework key-repeat downs: a held key re-sends
                // ACTION_DOWN every ~50 ms, and treating each as a fresh press
                // would reset the hold timer forever (the 1 s mute would never
                // fire) and make the eventual release look like a short press.
                if (event.repeatCount > 0) return true
                micButtonDownAt.set(event.eventTime)
                handler.removeCallbacks(holdTask)
                handler.postDelayed(holdTask, MIC_HOLD_MS)
            }
            KeyEvent.ACTION_UP -> {
                val downAt = micButtonDownAt.getAndSet(0L)
                handler.removeCallbacks(holdTask)
                val heldMs = event.eventTime - downAt
                when {
                    // Short press, judged on release (< 700 ms): talk toggle.
                    heldMs < MIC_SHORT_MS -> shortPress()
                    // 700 ms-1 s: between the two meanings -- nothing. The
                    // hold (>= 1 s) already fired at its own threshold while
                    // still held; a release after that changes nothing.
                    else -> {}
                }
            }
            else -> {}
        }
        return true
    }

    /**
     * Short press: talk toggle. Idle -> start a conversation; a conversation
     * running -> end it; muted -> unmute AND start talking.
     */
    private fun shortPress() {
        if (privacyMuted) {
            Log.i(TAG, "mic button: short press while muted -> unmute + talk")
            applyPrivacyMute(false)
        }
        if (link?.state != Link.State.CONNECTED) {
            Log.i(TAG, "mic button ignored: link is ${link?.state}")
            return
        }
        if (statusBar.state != Protocol.UiState.IDLE) {
            Log.i(TAG, "mic button: ending the conversation")
            link?.sendButton("talk_toggle")
        } else {
            Log.i(TAG, "mic button: talk")
            link?.sendButton("talk_toggle")
        }
    }

    // --- privacy mute (device-owned) ---------------------------------------

    /** True while the device is deaf: no audio leaves it, wake is off. */
    var privacyMuted: Boolean = false
        private set

    /** Apply the privacy-mute transition locally; persists across reboots. */
    private fun applyPrivacyMute(on: Boolean) {
        if (privacyMuted == on) return
        privacyMuted = on
        capture?.muted = on
        statusBar.micMuted = on
        prefs.edit().putBoolean(PREF_MUTED, on).apply()
        led?.setMuted(on)
        // The WebView home shows a rail mute chip while the device is deaf
        // (UI-BRIEF-14). Calls queue until the page is ready, so a muted boot
        // still lands the chip.
        web?.mute(on)
        Log.i(TAG, if (on) "privacy MUTE on" else "privacy mute off")
    }

    /** Hold (>= 1 s): flip the mute. Ends any active session first. */
    private fun togglePrivacyMute() {
        if (statusBar.state != Protocol.UiState.IDLE) {
            // A hold while talking ends the session and mutes.
            link?.sendStop()
        }
        applyPrivacyMute(!privacyMuted)
        link?.sendButton("mute")
    }

    // --- listening cue ------------------------------------------------------

    /**
     * A short double-beep on STREAM_SYSTEM so it sits above whatever else is
     * playing (music pauses during conversation anyway). Confirmation that the
     * wake word or the mic button registered, for the moments the screen is
     * out of view.
     */
    private fun playListeningCue() {
        try {
            val generator = ToneGenerator(AudioManager.STREAM_SYSTEM, 65)
            generator.startTone(ToneGenerator.TONE_PROP_ACK, 90)
            handler.postDelayed({
                try {
                    generator.startTone(ToneGenerator.TONE_PROP_ACK, 90)
                } catch (_: RuntimeException) {
                }
            }, 140)
            handler.postDelayed({ generator.release() }, 500)
        } catch (_: RuntimeException) {
            // No tone hardware or AudioManager busy -- the visual ring still
            // shows the state; a missing cue must never break a session.
        }
    }

    /** The alarms/timers entry point, from a native tile or a WebView chip. */
    private fun openSchedule(kind: String) {
        push?.fadeToClock(kind)
        web?.displayClear()
        scheduleScreen.open(kind)
    }

    /**
     * What the ambient page is allowed to ask for. [AmbientWeb.Callbacks] is
     * the whole surface: three intents and a failure report.
     *
     * Note there is no separate "end the conversation" intent. Since v1.4 a tap
     * is a tap wherever it lands, and [handleTap] is the single place that
     * decides what it means -- the page must not be able to reach a second,
     * divergent rule.
     *
     * [onMedia] is the exception that proves it: the transport buttons on the
     * now-playing card are a page interaction and mark themselves as such, so
     * they never reach [handleTap] and pressing pause does not also start a
     * conversation.
     */
    private val webCallbacks = object : AmbientWeb.Callbacks {
        override fun onTap() = handleTap()

        override fun onOpenAlarm() = openSchedule("alarm")
        override fun onOpenTimer() = openSchedule("timer")
        override fun onOpenStopwatch() = openSchedule("stopwatch")

        override fun onMedia(action: Protocol.MediaAction) {
            // Nothing changes locally. The player is on the PC, so this is a
            // request; the card redraws when the server's next now_playing push
            // says what actually happened.
            if (link?.state != Link.State.CONNECTED) {
                Log.i(TAG, "media ${action.wire} ignored: link is ${link?.state}")
                return
            }
            Log.i(TAG, "media control: ${action.wire}")
            link?.sendMedia(action)
        }

        override fun onWebViewFailed(reason: String) = enterCanvasMode(reason)
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
                // A device that booted muted starts deaf (HARDWARE-BRIEF-7 v2):
                // the gate must be applied at creation, not just at toggle time.
                recorder.muted = privacyMuted
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
            listener = this,
            mutedProvider = { privacyMuted }
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
            web?.linkState(state)
            if (state != Link.State.CONNECTED) {
                // Link loss returns the display to the clock: pushed content is
                // by definition stale once the thing that pushed it is gone.
                push?.fadeToClock("link lost")
                web?.displayClear()
                statusBar.state = Protocol.UiState.IDLE
                web?.uiState(Protocol.UiState.IDLE)
                statusBar.cameraStreaming = false
                statusBar.shutterClosed = false
                // No server, no session to end -- and no way to send the stop
                // if the hint were tapped.
                statusBar.clearQuietWindow()
                web?.quietWindow(false)
                capture?.uplinkEnabled = true
                playback?.flush()
                // Buffered music from a server that is no longer there is by
                // definition the past; playing it out after a reconnect is
                // worse than the silence.
                music?.flush()
                // And the camera goes with it. No server means no session.
                camera?.release()
            }
        }
    }

    override fun onControl(msg: Protocol.Incoming) {
        when (msg.type) {
            Protocol.Type.ALARM_COMMAND -> runOnUiThread {
                val body = msg.body
                val op = body.optString("op", "")
                // Stopwatch ops ride the alarm_command channel (v1.6,
                // CLOCK-BRIEF-STOPWATCH); route them to the stopwatch, whose
                // reply is the state ack the server's tool round trip waits on.
                if (op.startsWith("stopwatch_")) {
                    link?.sendControl(stopwatch.command(body))
                } else {
                    link?.sendControl(scheduler.command(body))
                }
            }
            Protocol.Type.WELCOME -> Log.i(
                TAG,
                "server welcome: live=${msg.bool("live_enabled")} " +
                    "audio=${msg.int("audio_up_rate")}->${msg.int("audio_down_rate")} Hz"
            )

            Protocol.Type.STATE -> runOnUiThread {
                val prev = statusBar.state
                val next = Protocol.UiState.from(msg.str("state"))
                statusBar.state = next
                web?.uiState(next)
                // The session is over; there is nothing left to keep talking to.
                if (next == Protocol.UiState.IDLE) {
                    statusBar.clearQuietWindow()
                    web?.quietWindow(false)
                }
                // A fresh conversation opening (wake word heard, or the mic
                // button) gets an audio cue: the screen is not always in view,
                // and "did it hear me?" needs an answer you can hear. Only the
                // idle -> listening transition cues -- mid-chat re-listens
                // would ding at every pause.
                if (next == Protocol.UiState.LISTENING && prev == Protocol.UiState.IDLE) {
                    playListeningCue()
                }
            }

            // v1.6: voice-initiated mute ("Jarvis, go mute"). The server asks
            // for the full desired state; the device owns the gate.
            Protocol.Type.MUTE -> runOnUiThread {
                applyPrivacyMute(msg.bool("on", false))
            }

            // v1.6 (UI-BRIEF-14): the server's focus computation (chat while a
            // session runs, music while the speaker card is up, else home)
            // picks the layout template. Unknown foci fall back to home in the
            // WebView. The Canvas fallback shows the clock for all foci.
            Protocol.Type.LAYOUT -> runOnUiThread {
                web?.layout(msg.str("focus", "home"))
            }

            // The model has finished answering and the server is counting down
            // to closing the session (POST_ANSWER_SILENCE_S). Re-sent whenever
            // the deadline moves, so a tap that extends it retracts the hint.
            Protocol.Type.SESSION_QUIET -> runOnUiThread {
                val active = msg.bool("active", false)
                if (active) {
                    statusBar.setQuietWindow((msg.dbl("closes_in_s", 0.0) * 1000).toLong())
                } else {
                    statusBar.clearQuietWindow()
                }
                web?.quietWindow(active)
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
                    runOnUiThread {
                        val webView = web
                        if (webView != null) webView.display(cmd) else push?.show(cmd)
                    }
                }
            }

            Protocol.Type.DISPLAY_CLEAR -> runOnUiThread {
                push?.fadeToClock("cleared")
                web?.displayClear()
            }

            // Ambient decoration, and treated as such: an unreadable payload is
            // logged and dropped, never answered with an error and never
            // allowed to clear a good reading the card is already showing.
            Protocol.Type.WEATHER -> {
                val reading = Protocol.Weather.from(msg)
                if (reading == null) {
                    Log.w(TAG, "unusable weather payload; keeping the last reading")
                } else {
                    runOnUiThread {
                        // Both layers, always: the Canvas ambient has to have
                        // the last reading in hand if it is ever handed the
                        // screen mid-session.
                        clock.weather = reading
                        web?.weather(reading)
                    }
                }
            }

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

    override fun onMusicDown(pcm: ByteArray) {
        music?.enqueue(pcm)
    }

    private fun updateTiles() {
        val state = JSONObject(scheduler.snapshot())
        val alarms = state.getJSONArray("alarms").length()
        val timers = state.getJSONArray("timers").length()
        alarmTile.text = "Alarms · $alarms"
        timerTile.text = "Timers · $timers"
        web?.schedule(alarms, timers)
        pushStopwatchChip()
    }

    /** Stopwatch tile + WebView chip, throttled to seconds-resolution changes. */
    private fun pushStopwatchChip() {
        val ms = stopwatch.elapsed()
        val totalS = ms / 1000
        val text = "%02d:%02d".format(totalS / 60, totalS % 60)
        stopwatchTile.text = if (stopwatch.running) "Stopwatch · $text" else "Stopwatch"
        if (text != lastStopwatchChip || stopwatch.running != lastStopwatchRunning) {
            lastStopwatchChip = text
            lastStopwatchRunning = stopwatch.running
            web?.stopwatch(stopwatch.running, text)
        }
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

        /** Short-press threshold: under this (on release) = talk toggle. */
        const val MIC_SHORT_MS = 700L

        /** Hold threshold: at this (while still held) = mute toggle. */
        const val MIC_HOLD_MS = 1_000L

        const val PREF_MUTED = "privacy_muted"
    }
}
