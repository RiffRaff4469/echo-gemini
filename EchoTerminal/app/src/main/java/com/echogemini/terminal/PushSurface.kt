package com.echogemini.terminal

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import org.json.JSONObject
import java.util.Locale

/**
 * The WebView layer that sits above the ambient clock and renders pushed
 * content, then fades back to the clock on expiry or link loss.
 *
 * A WebView is the right choice for the push surface specifically because it
 * makes "anything via API" true (HANDOFF section 8.1) -- weather, calendar,
 * photos, transit, dashboards -- without shipping a new APK per card type. All
 * four display types are rendered as HTML here; `text` and `timer` get a
 * template so the common cases do not require the caller to write markup.
 */
@SuppressLint("SetJavaScriptEnabled")
class PushSurface(context: Context) : FrameLayout(context) {

    private val web = WebView(context).apply {
        setBackgroundColor(Color.BLACK)
        isVerticalScrollBarEnabled = false
        isHorizontalScrollBarEnabled = false
        // The surface never navigates: everything is pushed in as a string.
        webViewClient = WebViewClient()
        settings.apply {
            // On for the timer countdown template and for dashboard content
            // people will inevitably push. Nothing here exposes a JS bridge, so
            // the WebView cannot reach the app or the tailnet credentials.
            javaScriptEnabled = true
            domStorageEnabled = false
            allowFileAccess = false
            allowContentAccess = false
            // Images come from URLs the caller chose; on 1 GB of RAM, cache
            // rather than re-fetch on every push.
            loadsImagesAutomatically = true
            mediaPlaybackRequiresUserGesture = true
            setSupportZoom(false)
            builtInZoomControls = false
        }
    }

    private val handler = Handler(Looper.getMainLooper())
    private var expiry: Runnable? = null

    /** Highest priority currently on screen; a lower-priority push is ignored. */
    private var activePriority = Int.MIN_VALUE
    private var visibleContent = false

    init {
        addView(
            web,
            LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT)
        )
        alpha = 0f
        visibility = View.GONE
    }

    /**
     * Render a display command. Returns false if it was dropped because
     * something higher-priority is already showing.
     */
    fun show(cmd: Protocol.DisplayCommand): Boolean {
        if (visibleContent && cmd.priority < activePriority) {
            Log.i(TAG, "ignoring priority ${cmd.priority} push; ${activePriority} is showing")
            return false
        }
        val html = try {
            render(cmd)
        } catch (e: Exception) {
            Log.e(TAG, "could not render ${cmd.type.wire} payload", e)
            return false
        }

        activePriority = cmd.priority
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
        reveal()

        cancelExpiry()
        if (cmd.durationMs > 0) {
            val job = Runnable { fadeToClock("expired") }
            expiry = job
            handler.postDelayed(job, cmd.durationMs)
        }
        return true
    }

    /** Fade back to the clock. Called on `display_clear`, expiry, or link loss. */
    fun fadeToClock(reason: String) {
        cancelExpiry()
        if (!visibleContent) return
        Log.i(TAG, "returning to the ambient clock ($reason)")
        visibleContent = false
        activePriority = Int.MIN_VALUE
        animate().alpha(0f).setDuration(FADE_MS).withEndAction {
            visibility = View.GONE
            // Release the rendered page so a pushed dashboard is not sitting in
            // 1 GB of RAM for the rest of the week.
            web.loadUrl("about:blank")
        }.start()
    }

    private fun reveal() {
        if (visibleContent) return
        visibleContent = true
        visibility = View.VISIBLE
        animate().alpha(1f).setDuration(FADE_MS).start()
    }

    private fun cancelExpiry() {
        expiry?.let { handler.removeCallbacks(it) }
        expiry = null
    }

    fun destroy() {
        cancelExpiry()
        web.loadUrl("about:blank")
        web.destroy()
    }

    // --- templates ----------------------------------------------------------

    private fun render(cmd: Protocol.DisplayCommand): String = when (cmd.type) {
        Protocol.DisplayType.HTML -> cmd.payload.getString("html")
        Protocol.DisplayType.TEXT -> textTemplate(cmd.payload)
        Protocol.DisplayType.IMAGE -> imageTemplate(cmd.payload)
        Protocol.DisplayType.TIMER -> timerTemplate(cmd.payload)
        Protocol.DisplayType.NOW_PLAYING -> nowPlayingTemplate(cmd.payload)
        // Canvas fallback (no WebView): options/list degrade to a plain,
        // non-interactive text card. The WebView path renders them as real
        // touch panels; this is the emergency-only floor (UI-BRIEF-16).
        Protocol.DisplayType.OPTIONS -> panelTemplate(
            cmd.payload.optString("question").ifEmpty { cmd.payload.optString("title") },
            optionLabels(cmd.payload)
        )
        Protocol.DisplayType.LIST -> panelTemplate(
            cmd.payload.optString("title"),
            listItems(cmd.payload)
        )
    }

    private fun optionLabels(p: JSONObject): List<String> {
        val out = mutableListOf<String>()
        val arr = p.optJSONArray("options") ?: return out
        for (i in 0 until arr.length()) {
            out += arr.optJSONObject(i)?.optString("label") ?: continue
        }
        return out
    }

    private fun listItems(p: JSONObject): List<String> {
        val out = mutableListOf<String>()
        val arr = p.optJSONArray("items") ?: return out
        for (i in 0 until arr.length()) out += arr.optString(i)
        return out
    }

    /** Read-only rendering of an options/list panel for the canvas fallback. */
    private fun panelTemplate(header: String, lines: List<String>): String {
        val head = if (header.isEmpty()) "" else "<div class='main'>${escape(header)}</div>"
        val body = lines.mapIndexed { i, text ->
            "<div class='row'><span class='n'>${i + 1}.</span>${escape(text)}</div>"
        }.joinToString("")
        return page("""
            <div class='wrap'>$head<div class='rows'>$body</div></div>
        """)
    }

    private fun nowPlayingTemplate(p: JSONObject): String {
        val duration = p.optDouble("duration_s", 0.0).coerceAtLeast(0.0)
        val progress = p.optDouble("progress_s", 0.0).coerceIn(0.0, duration)
        val art = p.optString("art_url")
        val image = if (art.isEmpty()) "" else "<img style='height:35vh' src=\"${escape(art)}\" alt='Album art'>"
        return page("""
            <div class='wrap'>$image
              <div class='main'>${escape(p.getString("title"))}</div>
              <div class='sub'>${escape(p.optString("artist"))} &middot; ${escape(p.optString("album"))}</div>
              <progress style='width:70%;margin-top:3vh' max='${duration.coerceAtLeast(1.0)}' value='$progress'></progress>
              <div class='sub'>${if (p.optBoolean("is_playing", true)) "Now playing" else "Paused"}</div>
            </div>
        """)
    }

    private fun textTemplate(payload: JSONObject): String {
        val text = escape(payload.getString("text"))
        val subtitle = payload.optString("subtitle", "")
        val subtitleHtml =
            if (subtitle.isEmpty()) ""
            else "<div class='sub'>${escape(subtitle)}</div>"
        return page(
            """
            <div class='wrap'>
              <div class='main'>$text</div>
              $subtitleHtml
            </div>
            """
        )
    }

    private fun imageTemplate(payload: JSONObject): String {
        // The URL is attribute-quoted and escaped; the server has already
        // rejected anything that is not http(s) or a data:image URI.
        val url = escape(payload.getString("url"))
        return page("<div class='wrap'><img src=\"$url\" alt=\"\"></div>")
    }

    private fun timerTemplate(payload: JSONObject): String {
        val label = escape(payload.getString("label"))
        val seconds = payload.optDouble("seconds", 0.0).toLong()
        // Counted down in the page so it stays live without the server having
        // to push a new command every second.
        return page(
            """
            <div class='wrap'>
              <div class='sub'>$label</div>
              <div class='main' id='t'>--:--</div>
            </div>
            <script>
              var left = $seconds;
              function pad(n){ return (n < 10 ? '0' : '') + n; }
              function draw(){
                var s = Math.max(0, left);
                var text = Math.floor(s / 60) + ':' + pad(s % 60);
                if (s >= 3600) {
                  text = Math.floor(s / 3600) + ':' + pad(Math.floor(s / 60) % 60)
                         + ':' + pad(s % 60);
                }
                document.getElementById('t').textContent = text;
                if (left <= 0) { document.getElementById('t').style.color = '#E4574C'; return; }
                left -= 1;
                setTimeout(draw, 1000);
              }
              draw();
            </script>
            """
        )
    }

    /** Shared chrome, sized for a 960x480 panel read from across a room. */
    private fun page(body: String): String = """
        <!doctype html>
        <html><head>
        <meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
        <style>
          html,body { margin:0; padding:0; height:100%; background:#000; color:#F2F4F8;
                      font-family:sans-serif; overflow:hidden; }
          .wrap { height:100%; display:flex; flex-direction:column;
                  align-items:center; justify-content:center; text-align:center;
                  padding:0 4vw; box-sizing:border-box; }
          .main { font-size:12vh; font-weight:300; line-height:1.05; }
          .sub  { font-size:5vh; color:#B9C2CF; margin-top:2vh; }
          img   { max-width:100%; max-height:100%; object-fit:contain; }
          .rows { margin-top:2vh; font-size:4vh; text-align:left; }
          .row  { margin:1vh 0; }
          .n    { display:inline-block; width:2.4em; color:#7C8794; }
        </style>
        </head><body>$body</body></html>
    """.trimIndent()

    private fun escape(raw: String): String = raw
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&#39;")

    companion object {
        private const val TAG = "EchoPushSurface"
        private const val FADE_MS = 320L

        /** Exposed for readability in logs. */
        fun describe(cmd: Protocol.DisplayCommand): String = String.format(
            Locale.US,
            "%s duration=%dms priority=%d",
            cmd.type.wire, cmd.durationMs, cmd.priority
        )
    }
}
