package com.echogemini.terminal

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.view.View
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * The base layer: a locally-rendered clock with **zero network dependency**.
 *
 * This is the explicit product requirement (HANDOFF section 8.1): if the
 * Windows PC is off, unplugged, or has never existed, the Show is still a
 * clock. Nothing in this class touches [Link], reads a socket, or waits on
 * anything. It draws the system time on a handler tick and that is all.
 *
 * Verification step 12 tests exactly this, by deploying the app with the server
 * stopped -- "test it by making it fail".
 *
 * Sized for a 960x480 panel read from across a room, not held at arm's length.
 */
class AmbientClock(context: Context) : View(context) {

    /** Purely cosmetic -- the clock renders identically whatever this says. */
    enum class LinkIndicator { OFFLINE, CONNECTING, ONLINE }

    private val timePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        typeface = Typeface.create("sans-serif-thin", Typeface.NORMAL)
        textAlign = Paint.Align.CENTER
    }

    private val datePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#B9C2CF")
        typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        textAlign = Paint.Align.CENTER
    }

    private val secondsPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#6E7A8A")
        typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }

    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    private val timeFormat = SimpleDateFormat("h:mm", Locale.getDefault())
    private val secondsFormat = SimpleDateFormat("ss", Locale.getDefault())
    private val meridiemFormat = SimpleDateFormat("a", Locale.getDefault())
    private val dateFormat = SimpleDateFormat("EEEE, d MMMM", Locale.getDefault())

    private val handler = Handler(Looper.getMainLooper())
    private var ticking = false

    /**
     * Night dimming. HANDOFF section 8.1: the panel is LCD so burn-in is not a
     * concern, but a clock that blazes at 3 a.m. is its own problem. Driven by
     * the light sensor in [MainActivity], with a clock-hour fallback so it
     * still dims on a unit whose sensor misbehaves.
     */
    var dimFactor: Float = 1f
        set(value) {
            val clamped = value.coerceIn(MIN_DIM, 1f)
            if (kotlin.math.abs(clamped - field) < 0.02f) return
            field = clamped
            invalidate()
        }

    var indicator: LinkIndicator = LinkIndicator.OFFLINE
        set(value) {
            if (field == value) return
            field = value
            invalidate()
        }

    private val tick = object : Runnable {
        override fun run() {
            invalidate()
            if (ticking) {
                // Re-align to the next second boundary so the seconds readout
                // does not drift or stutter.
                handler.postDelayed(this, 1000L - (System.currentTimeMillis() % 1000L))
            }
        }
    }

    fun startTicking() {
        if (ticking) return
        ticking = true
        handler.post(tick)
    }

    fun stopTicking() {
        ticking = false
        handler.removeCallbacks(tick)
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        startTicking()
    }

    override fun onDetachedFromWindow() {
        stopTicking()
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        val now = Date()

        canvas.drawColor(Color.BLACK)

        timePaint.textSize = h * 0.42f
        datePaint.textSize = h * 0.09f
        secondsPaint.textSize = h * 0.12f

        applyDim(timePaint, 0xFFFFFF)
        applyDim(datePaint, 0xB9C2CF)
        applyDim(secondsPaint, 0x6E7A8A)

        val timeText = timeFormat.format(now)
        val centreY = h * 0.55f
        canvas.drawText(timeText, w / 2f, centreY, timePaint)

        // Seconds and AM/PM sit to the right of the hour:minute block, small,
        // so the big glanceable number stays uncluttered.
        val timeWidth = timePaint.measureText(timeText)
        val gutterX = (w / 2f) + (timeWidth / 2f) + (w * 0.02f)
        canvas.drawText(secondsFormat.format(now), gutterX, centreY - h * 0.20f, secondsPaint)
        canvas.drawText(
            meridiemFormat.format(now).uppercase(Locale.getDefault()),
            gutterX,
            centreY - h * 0.04f,
            secondsPaint
        )

        canvas.drawText(dateFormat.format(now), w / 2f, h * 0.78f, datePaint)

        drawIndicator(canvas, w, h)
    }

    /**
     * A small dot, bottom-left. Deliberately subtle: the link being down is not
     * an error the household needs shouting at them, and the clock is fully
     * functional either way.
     */
    private fun drawIndicator(canvas: Canvas, w: Float, h: Float) {
        val colour = when (indicator) {
            LinkIndicator.ONLINE -> 0x4FA96B
            LinkIndicator.CONNECTING -> 0xB08A3C
            LinkIndicator.OFFLINE -> 0x3A4250
        }
        applyDim(dotPaint, colour)
        val radius = h * 0.012f
        canvas.drawCircle(w * 0.035f, h * 0.93f, radius, dotPaint)
    }

    private fun applyDim(paint: Paint, rgb: Int) {
        val r = ((rgb shr 16 and 0xFF) * dimFactor).toInt().coerceIn(0, 255)
        val g = ((rgb shr 8 and 0xFF) * dimFactor).toInt().coerceIn(0, 255)
        val b = ((rgb and 0xFF) * dimFactor).toInt().coerceIn(0, 255)
        paint.color = Color.rgb(r, g, b)
    }

    companion object {
        private const val MIN_DIM = 0.22f

        /**
         * Fallback dimming curve for a unit whose light sensor is unreliable.
         * Full brightness in the day, deeply dimmed in the small hours.
         */
        fun dimForHour(calendar: Calendar = Calendar.getInstance()): Float =
            when (calendar.get(Calendar.HOUR_OF_DAY)) {
                in 0..5 -> 0.30f
                6 -> 0.60f
                in 7..20 -> 1.00f
                21 -> 0.75f
                else -> 0.45f
            }
    }
}
