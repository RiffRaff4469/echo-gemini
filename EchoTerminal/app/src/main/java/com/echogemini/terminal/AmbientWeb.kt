package com.echogemini.terminal

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import org.json.JSONObject

/**
 * The ambient display, hosted in a WebView (UI-BRIEF-9).
 *
 * The UI itself is `assets/ambient/` -- plain HTML, CSS and JS ported from
 * `docs/ui-mockup/mockup.html`. Nothing about the app's plumbing moved: the
 * link, audio, camera, alarms and the schedule screens are all still native,
 * and this class is only a display surface plus a deliberately small bridge.
 *
 * ## Why a WebView at all
 *
 * The mockup is already zero-dependency HTML/CSS/JS, page cycling and the
 * StandBy styling are a few lines of JS each, and once the UI is an asset a
 * visual change is a file edit rather than an APK. The cost is that it is one
 * more thing that can be missing on a custom ROM -- see below.
 *
 * ## The bridge, and what is deliberately not on it
 *
 * JS can call exactly five things: [tap], [openAlarm], [openTimer], [media] and
 * [log]. There is no shell, no file access, no link handle and no way to
 * send an arbitrary control message -- [media] takes one of five fixed action
 * words and anything else is dropped here rather than forwarded. Server-pushed
 * `html` cards are rendered by the page inside a sandboxed iframe with no
 * `allow-same-origin`, so a pushed `<script>` gets an opaque origin and cannot
 * see this object at all.
 *
 * ## Failing back
 *
 * The ROM being built has the system WebView removed. Construction, load
 * errors, a dead renderer and a load that never finishes are all reported
 * through [Callbacks.onWebViewFailed], and MainActivity answers by restoring
 * the Canvas ambient from UI-BRIEF-4. That path stays compiled in and is the
 * only display on any device where this class cannot start.
 */
@SuppressLint("SetJavaScriptEnabled")
class AmbientWeb private constructor(
    context: Context,
    private val callbacks: Callbacks,
    private val web: WebView
) : FrameLayout(context) {

    interface Callbacks {
        /**
         * A touch anywhere the page did not claim. What it means -- open a
         * session, or end the one on screen -- is decided by the host, not
         * here: see `MainActivity.handleTap`.
         */
        fun onTap()
        fun onOpenAlarm()
        fun onOpenTimer()

        /** A transport button on the now-playing card (protocol v1.5). */
        fun onMedia(action: Protocol.MediaAction)

        /** The WebView is unusable; fall back to the Canvas ambient. */
        fun onWebViewFailed(reason: String)
    }

    private val handler = Handler(Looper.getMainLooper())

    /** Calls made before the page finishes loading, replayed in order. */
    private val pending = ArrayList<String>()
    private var ready = false
    private var failed = false

    /** A load that never finishes is as broken as one that errors. */
    private val loadWatchdog = Runnable {
        if (!ready) fail("the ambient page did not finish loading in ${LOAD_TIMEOUT_MS}ms")
    }

    init {
        setBackgroundColor(Color.BLACK)
        addView(web, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))

        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                handler.removeCallbacks(loadWatchdog)
                if (failed) return
                ready = true
                Log.i(TAG, "ambient UI loaded ($url)")
                pending.forEach { eval(it) }
                pending.clear()
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError
            ) {
                // Only the document itself is fatal. A card image that will
                // not load is the server's problem, not a reason to tear the
                // whole display down.
                if (request.isForMainFrame) fail("load error ${error.errorCode}")
            }

            override fun onRenderProcessGone(
                view: WebView,
                detail: RenderProcessGoneDetail?
            ): Boolean {
                // Returning true keeps the app alive; the WebView is dead
                // either way, so hand the screen back to the Canvas.
                fail("render process gone (crashed=${detail?.didCrash()})")
                return true
            }
        }

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = false
            // Assets remain reachable through file:///android_asset with file
            // access off; this only closes the rest of the filesystem.
            allowFileAccess = false
            allowContentAccess = false
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            // Without these two the page's `<meta viewport width=960>` is
            // ignored and the layout viewport becomes the panel's width in
            // density-independent pixels (~800 here), quietly rendering every
            // length in the stylesheet ~20% larger than the 960x480 the
            // mockup was drawn for. With them, one CSS pixel is one panel
            // pixel and the port is dimensionally exact.
            useWideViewPort = true
            loadWithOverviewMode = true
            // Not blocked: the ambient UI itself references nothing remote,
            // but pushed `image` and `now_playing` cards carry URLs the
            // server chose, and those loaded fine on the old PushSurface.
            loadsImagesAutomatically = true
            mediaPlaybackRequiresUserGesture = true
            setSupportZoom(false)
            builtInZoomControls = false
        }
        web.setBackgroundColor(Color.BLACK)
        web.isVerticalScrollBarEnabled = false
        web.isHorizontalScrollBarEnabled = false
        web.overScrollMode = OVER_SCROLL_NEVER

        web.addJavascriptInterface(Bridge(), "EchoNative")
        handler.postDelayed(loadWatchdog, LOAD_TIMEOUT_MS)
        web.loadUrl(ASSET_URL)
    }

    // --- failure ------------------------------------------------------------

    private fun fail(reason: String) {
        if (failed) return
        failed = true
        ready = false
        handler.removeCallbacks(loadWatchdog)
        Log.e(TAG, "WebView ambient failed: $reason")
        callbacks.onWebViewFailed(reason)
    }

    // --- native -> JS -------------------------------------------------------

    private fun call(js: String) {
        if (failed) return
        if (!ready) {
            pending.add(js)
            return
        }
        if (Looper.myLooper() == Looper.getMainLooper()) eval(js) else handler.post { eval(js) }
    }

    private fun eval(js: String) {
        try {
            web.evaluateJavascript(js, null)
        } catch (e: Exception) {
            fail("evaluateJavascript threw: $e")
        }
    }

    fun linkState(state: Link.State) {
        val wire = when (state) {
            Link.State.CONNECTED -> "online"
            Link.State.CONNECTING -> "connecting"
            Link.State.DISCONNECTED -> "offline"
        }
        call("Echo.linkState(${JSONObject.quote(wire)})")
    }

    fun uiState(state: Protocol.UiState) =
        call("Echo.uiState(${JSONObject.quote(state.wire)})")

    fun weather(reading: Protocol.Weather) {
        val json = JSONObject().apply {
            put("temp_c", reading.tempC)
            put("code", reading.code)
            put("is_day", reading.isDay)
        }
        call("Echo.weather(${JSONObject.quote(json.toString())})")
    }

    /** The post-answer countdown is running (or has stopped): show the hint. */
    fun quietWindow(active: Boolean) = call("Echo.quiet($active)")

    fun schedule(alarms: Int, timers: Int) = call("Echo.schedule($alarms, $timers)")

    fun display(cmd: Protocol.DisplayCommand) = call(
        "Echo.display(${JSONObject.quote(cmd.type.wire)}, " +
            "${JSONObject.quote(cmd.payload.toString())}, " +
            "${cmd.durationMs}, ${cmd.priority})"
    )

    fun displayClear() = call("Echo.displayClear()")

    /** Rows for a data page (BRIEF-10). Unknown pages are ignored by the JS. */
    fun pageData(page: String, json: String) =
        call("Echo.pageData(${JSONObject.quote(page)}, ${JSONObject.quote(json)})")

    fun destroy() {
        handler.removeCallbacks(loadWatchdog)
        web.removeJavascriptInterface("EchoNative")
        web.loadUrl("about:blank")
        web.destroy()
    }

    // --- JS -> native -------------------------------------------------------

    /**
     * Everything here arrives on the WebView's JavaScript thread, so every
     * method hops to the UI thread before touching anything.
     */
    private inner class Bridge {
        @JavascriptInterface
        fun tap() = handler.post { callbacks.onTap() }

        @JavascriptInterface
        fun openAlarm() = handler.post { callbacks.onOpenAlarm() }

        @JavascriptInterface
        fun openTimer() = handler.post { callbacks.onOpenTimer() }

        /**
         * The page's transport row. Validated to one of the five known actions
         * here rather than trusted: the bridge must not become a way to put an
         * arbitrary string on the wire, and an unrecognised word is a bug in
         * the asset, not something to forward and let the server puzzle over.
         */
        @JavascriptInterface
        fun media(action: String) {
            val parsed = Protocol.MediaAction.from(action)
            if (parsed == null) {
                Log.w(TAG, "ambient js asked for unknown media action: ${action.take(40)}")
                return
            }
            handler.post { callbacks.onMedia(parsed) }
        }

        /** The page's only way to say anything; it lands in logcat, nowhere else. */
        @JavascriptInterface
        fun log(message: String) {
            Log.i(TAG, "ambient js: ${message.take(400)}")
        }

        @JavascriptInterface
        fun ready() = Log.i(TAG, "ambient js is up")
    }

    companion object {
        private const val TAG = "EchoAmbientWeb"
        private const val ASSET_URL = "file:///android_asset/ambient/index.html"

        /** Generous: a cold WebView start on this hardware is not quick. */
        private const val LOAD_TIMEOUT_MS = 12_000L

        /**
         * Returns null when this device has no usable WebView at all --
         * which is the expected case on the ROM with the WebView stripped
         * out, where constructing one throws rather than returning an
         * unusable view.
         */
        fun createOrNull(context: Context, callbacks: Callbacks): AmbientWeb? = try {
            AmbientWeb(context, callbacks, WebView(context))
        } catch (e: Throwable) {
            Log.e(TAG, "no usable WebView on this device", e)
            null
        }
    }
}
