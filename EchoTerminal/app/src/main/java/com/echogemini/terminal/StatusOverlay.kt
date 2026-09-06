package com.echogemini.terminal

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.view.View

/**
 * The thin band across the top: conversation state, and the camera indicator.
 *
 * Sits above both the clock and the push surface, because the one thing that
 * must never be obscured by pushed content is the fact that the camera is on.
 *
 * **The camera indicator is a product requirement, not decoration.** HANDOFF
 * section 8.3: "a camera in a home streaming to a cloud model must be visibly
 * doing so." The physical shutter is the household's hard guarantee; this is
 * the software one. It is deliberately large, red, animated and impossible to
 * mistake for anything else.
 */
class StatusOverlay(context: Context) : View(context) {

    private val statePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }

    private val pillPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val camTextPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    private val hintPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }

    private val handler = Handler(Looper.getMainLooper())
    private var pulsePhase = 0f

    /** Reused: the camera pill repaints ~16 times a second while streaming. */
    private val pill = RectF()

    /** Where [drawState] finished, so the hint can sit just after the pill. */
    private var statePillRight = 0f

    var state: Protocol.UiState = Protocol.UiState.IDLE
        set(value) {
            if (field == value) return
            field = value
            invalidate()
        }

    /** True while JPEG frames are actually being sent to the server. */
    var cameraStreaming: Boolean = false
        set(value) {
            if (field == value) return
            field = value
            if (value) startPulse() else stopPulse()
            invalidate()
        }

    /** Shown instead of the camera pill when the privacy latch is engaged. */
    var shutterClosed: Boolean = false
        set(value) {
            if (field == value) return
            field = value
            invalidate()
        }

    private val pulse = object : Runnable {
        override fun run() {
            pulsePhase = (pulsePhase + 0.08f) % 1f
            invalidate()
            if (cameraStreaming) handler.postDelayed(this, 60)
        }
    }

    private fun startPulse() = handler.post(pulse)
    private fun stopPulse() = handler.removeCallbacks(pulse)

    // --- the post-answer hint -----------------------------------------------
    //
    // The server closes the session a few seconds after the model finishes
    // answering (POST_ANSWER_SILENCE_S) so a quick question does not leave the
    // microphone streaming while you walk away. That is the right default, but
    // it has to be escapable: tapping anywhere buys another half minute. This
    // is the only thing on screen that says so, and it is deliberately small,
    // muted and late -- the ambient home screen owns the display, and a
    // permanent "tap to keep talking" banner would be an advert for a feature
    // nobody needs to see most of the time.

    /** Elapsed-realtime deadline for the running window; 0 when none. */
    private var quietDeadlineMs = 0L
    private var hintAlpha = 0f

    private val hintTick = object : Runnable {
        override fun run() {
            val deadline = quietDeadlineMs
            if (deadline == 0L) return
            val remaining = deadline - android.os.SystemClock.elapsedRealtime()
            if (remaining <= 0L) {
                // The session is closing. The server's state message follows
                // and clears the band; nothing to fade out to.
                clearQuietWindow()
                return
            }
            val next = (HINT_LEAD_MS - remaining).coerceAtLeast(0L)
            hintAlpha = (next.toFloat() / HINT_FADE_MS).coerceIn(0f, 1f)
            invalidate()
            handler.postDelayed(this, if (remaining <= HINT_LEAD_MS) 80 else remaining - HINT_LEAD_MS)
        }
    }

    /**
     * Arm the hint against a window closing in [closesInMs].
     *
     * Called again whenever the server moves the deadline -- a tap extends it,
     * so the hint has to retreat rather than keep fading in over a session that
     * is no longer about to end.
     */
    fun setQuietWindow(closesInMs: Long) {
        handler.removeCallbacks(hintTick)
        quietDeadlineMs = android.os.SystemClock.elapsedRealtime() + closesInMs.coerceAtLeast(0L)
        hintAlpha = 0f
        invalidate()
        handler.post(hintTick)
    }

    /** No window is running: the conversation continued, or the session ended. */
    fun clearQuietWindow() {
        handler.removeCallbacks(hintTick)
        if (quietDeadlineMs == 0L && hintAlpha == 0f) return
        quietDeadlineMs = 0L
        hintAlpha = 0f
        invalidate()
    }

    override fun onDetachedFromWindow() {
        stopPulse()
        handler.removeCallbacks(hintTick)
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()

        drawState(canvas, h)
        if (hintAlpha > 0f) drawStayHint(canvas, h)

        when {
            cameraStreaming -> drawCameraPill(canvas, w, h)
            shutterClosed -> drawShutterPill(canvas, w, h)
        }
    }

    /**
     * "tap to keep talking", sitting just after the state pill.
     *
     * Left of the camera indicator by construction: the state pill is anchored
     * to the left edge and the camera pill to the right, so the hint grows into
     * the gap between them and can never cover the one thing on this band that
     * is a product requirement.
     */
    private fun drawStayHint(canvas: Canvas, h: Float) {
        hintPaint.textSize = h * 0.28f
        hintPaint.color = Color.argb(
            (150 * hintAlpha).toInt().coerceIn(0, 255), 226, 233, 244
        )
        canvas.drawText("tap to keep talking", statePillRight + h * 0.45f, h * 0.61f, hintPaint)
    }

    private fun drawState(canvas: Canvas, h: Float) {
        // IDLE draws nothing: an ambient clock should be a clock, not a
        // dashboard reminding you it is idle.
        statePillRight = h * 0.30f
        if (state == Protocol.UiState.IDLE) return

        val (label, colour) = when (state) {
            Protocol.UiState.LISTENING -> "LISTENING" to Color.parseColor("#63B3ED")
            Protocol.UiState.THINKING -> "THINKING" to Color.parseColor("#B794F4")
            Protocol.UiState.SPEAKING -> "SPEAKING" to Color.parseColor("#68D391")
            Protocol.UiState.IDLE -> return
        }

        // Sits in a soft pill tinted with its own colour, so the conversation
        // band belongs to the ambient home screen it floats over rather than
        // looking like text dropped on top of it. Same labels, same colours,
        // same trigger -- only the surround is new.
        statePaint.textSize = h * 0.36f
        val textWidth = statePaint.measureText(label)
        val left = h * 0.30f
        pill.set(left, h * 0.18f, left + textWidth + h * 1.10f, h * 0.82f)

        pillPaint.color = Color.argb(
            40, Color.red(colour), Color.green(colour), Color.blue(colour)
        )
        canvas.drawRoundRect(pill, h * 0.32f, h * 0.32f, pillPaint)
        statePillRight = pill.right

        statePaint.color = colour
        dotPaint.color = colour
        canvas.drawCircle(left + h * 0.36f, h * 0.5f, h * 0.12f, dotPaint)
        canvas.drawText(label, left + h * 0.60f, h * 0.63f, statePaint)
    }

    private fun drawCameraPill(canvas: Canvas, w: Float, h: Float) {
        // Pulsing so it reads as live, not as a static icon someone stops
        // noticing after a week.
        val breath = 0.72f + 0.28f * kotlin.math.abs(0.5f - pulsePhase) * 2f
        val red = Color.rgb((228 * breath).toInt().coerceIn(0, 255), 40, 46)

        camTextPaint.textSize = h * 0.40f
        val label = "CAMERA ON"
        val textWidth = camTextPaint.measureText(label)
        val pillWidth = textWidth + h * 1.5f
        val left = w - pillWidth - h * 0.3f

        pillPaint.color = red
        pill.set(left, h * 0.15f, w - h * 0.3f, h * 0.85f)
        canvas.drawRoundRect(pill, h * 0.35f, h * 0.35f, pillPaint)

        dotPaint.color = Color.WHITE
        canvas.drawCircle(left + h * 0.42f, h * 0.5f, h * 0.15f, dotPaint)
        canvas.drawText(label, left + h * 0.72f, h * 0.64f, camTextPaint)
    }

    private fun drawShutterPill(canvas: Canvas, w: Float, h: Float) {
        camTextPaint.textSize = h * 0.36f
        val label = "SHUTTER CLOSED"
        val textWidth = camTextPaint.measureText(label)
        val pillWidth = textWidth + h * 1.0f
        val left = w - pillWidth - h * 0.3f

        pillPaint.color = Color.parseColor("#4A5568")
        pill.set(left, h * 0.15f, w - h * 0.3f, h * 0.85f)
        canvas.drawRoundRect(pill, h * 0.35f, h * 0.35f, pillPaint)
        canvas.drawText(label, left + h * 0.5f, h * 0.63f, camTextPaint)
    }

    private companion object {
        /** How long before the close the hint starts appearing. */
        const val HINT_LEAD_MS = 3_000L

        /** Fade-in length. Shorter than the lead, so it settles before it matters. */
        const val HINT_FADE_MS = 700f
    }
}
