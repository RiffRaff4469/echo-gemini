package com.echogemini.terminal

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.View
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * The base layer: a locally-rendered ambient home screen with **zero network
 * dependency**.
 *
 * This is the explicit product requirement (HANDOFF section 8.1): if the
 * Windows PC is off, unplugged, or has never existed, the Show is still a
 * clock. Nothing in this class touches [Link], reads a socket, or waits on
 * anything. It draws the system time on a handler tick and that is all. The
 * one piece of server-fed content -- [weather] -- is a nullable field that
 * renders a placeholder when it has never been set.
 *
 * Verification step 12 tests exactly this, by deploying the app with the server
 * stopped -- "test it by making it fail".
 *
 * ## What is drawn, back to front
 *
 * ```
 *   sky gradient   time-of-day colours, interpolated continuously
 *   aurora glow    one soft radial blob, drifting on a ~7 minute cycle
 *   clock + date   centred, thin, sized to read from across the room
 *   weather card   bottom left, translucent, placeholder until pushed
 *   link dot       bottom centre, tiny, labelled only when NOT connected
 * ```
 *
 * ## Cost
 *
 * A 1 GB always-on device. Everything here is bounded by that: one tick per
 * second (the drift is slow enough that 1 fps reads as continuous), no
 * allocation in [onDraw] beyond the formatters' own, and both shaders cached
 * until the size or the interpolated colours actually change -- which during
 * the flat middle of the day or night is never. No RenderEffect, no
 * RuntimeShader, no bitmap layers.
 *
 * Sized for a 960x480 panel read from across a room, not held at arm's length.
 */
class AmbientClock(context: Context) : View(context) {

    /** Purely cosmetic -- the clock renders identically whatever this says. */
    enum class LinkIndicator { OFFLINE, CONNECTING, ONLINE }

    // --- paints -------------------------------------------------------------
    // All allocated once. onDraw only ever mutates colour and text size.

    private val skyPaint = Paint()
    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    private val timePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif-thin", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
        // Lifts the thin strokes off a light dusk gradient without turning the
        // numerals grey. Text shadows are the one shadow layer that is reliably
        // hardware-accelerated.
        setShadowLayer(18f, 0f, 6f, Color.argb(120, 0, 0, 0))
    }

    private val meridiemPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }

    private val datePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        textAlign = Paint.Align.CENTER
    }

    private val cardFillPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val cardStrokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
    }
    private val tempPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }
    private val conditionPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }
    private val glyphFillPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glyphStrokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }

    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val offlinePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        textAlign = Paint.Align.LEFT
    }

    // Reused geometry. A RectF or Path allocated per frame is 86 400 objects a
    // day on a device with 1 GB of RAM.
    private val rect = RectF()
    private val moonPath = Path()
    private val moonOuter = Path()
    private val moonInner = Path()
    private val boltPath = Path()
    private var glyphPathRadius = -1f

    private val timeFormat = SimpleDateFormat("h:mm", Locale.getDefault())
    private val meridiemFormat = SimpleDateFormat("a", Locale.getDefault())
    private val dateFormat = SimpleDateFormat("EEEE, d MMMM", Locale.getDefault())

    private val handler = Handler(Looper.getMainLooper())
    private var ticking = false

    // --- cached shaders -----------------------------------------------------

    private var skyTop = 0
    private var skyMid = 0
    private var skyBottom = 0
    private var shadedWidth = 0
    private var shadedHeight = 0
    private var glowRadius = 0f

    /**
     * Night dimming. HANDOFF section 8.1: the panel is LCD so burn-in is not a
     * concern, but a clock that blazes at 3 a.m. is its own problem. Driven by
     * the light sensor in [MainActivity], with a clock-hour fallback so it
     * still dims on a unit whose sensor misbehaves.
     */
    var dimFactor: Float = 1f
        set(value) {
            val clamped = value.coerceIn(MIN_DIM, 1f)
            if (abs(clamped - field) < 0.02f) return
            field = clamped
            invalidate()
        }

    var indicator: LinkIndicator = LinkIndicator.OFFLINE
        set(value) {
            if (field == value) return
            field = value
            invalidate()
        }

    /**
     * The latest observation the server pushed, or null if it never has.
     *
     * Null is a normal, long-lived state -- the PC being off is normal (HANDOFF
     * 8.1) -- so the card renders a placeholder rather than disappearing. A
     * card that comes and goes would make the layout jump every time the link
     * flaps.
     */
    var weather: Protocol.Weather? = null
        set(value) {
            field = value
            invalidate()
        }

    private val tick = object : Runnable {
        override fun run() {
            invalidate()
            if (ticking) {
                // Re-align to the next second boundary so the minute rolls over
                // when it should rather than drifting up to a second late.
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
        if (w <= 0f || h <= 0f) return
        val now = Date()

        drawSky(canvas, w, h)
        drawClock(canvas, w, h, now)
        drawWeatherCard(canvas, w, h)
        drawLinkDot(canvas, w, h)
    }

    // --- sky ----------------------------------------------------------------

    /**
     * The time-of-day gradient plus one drifting glow.
     *
     * Colours are interpolated between the keyframes in [SKY] on a continuous
     * fractional hour, so there is no moment where the screen visibly steps
     * from one palette to the next -- the whole point of an always-on display
     * someone walks past a hundred times a day.
     */
    private fun drawSky(canvas: Canvas, w: Float, h: Float) {
        val calendar = Calendar.getInstance()
        val hour = calendar.get(Calendar.HOUR_OF_DAY) +
            calendar.get(Calendar.MINUTE) / 60f +
            calendar.get(Calendar.SECOND) / 3600f

        // The backdrop dims with the room but never all the way to black: a
        // pure black panel at 3 a.m. reads as "off", not as "night".
        val backdropDim = 0.55f + 0.45f * dimFactor
        val top = skyColour(hour, 0, backdropDim)
        val mid = skyColour(hour, 1, backdropDim)
        val bottom = skyColour(hour, 2, backdropDim)

        val sizeChanged = shadedWidth != width || shadedHeight != height
        if (sizeChanged || top != skyTop || mid != skyMid || bottom != skyBottom) {
            skyTop = top
            skyMid = mid
            skyBottom = bottom
            shadedWidth = width
            shadedHeight = height
            skyPaint.shader = LinearGradient(
                0f, 0f, w * 0.25f, h,
                intArrayOf(top, mid, bottom),
                floatArrayOf(0f, 0.55f, 1f),
                Shader.TileMode.CLAMP
            )
            // The glow is the sky's own bottom colour lifted towards white, so
            // it always belongs to the palette it is sitting on.
            glowRadius = max(w, h) * 0.62f
            glowPaint.shader = RadialGradient(
                0f, 0f, glowRadius,
                lighten(bottom, 0.42f, GLOW_ALPHA),
                Color.TRANSPARENT,
                Shader.TileMode.CLAMP
            )
        }

        canvas.drawRect(0f, 0f, w, h, skyPaint)

        // One soft blob on a slow triangular path. At 1 fps it moves about
        // three pixels a frame, which on a blob this diffuse is invisible as
        // motion and reads only as "the light in here changes".
        val phase = (SystemClock.elapsedRealtime() % DRIFT_PERIOD_MS) / DRIFT_PERIOD_MS.toFloat()
        val sweep = 1f - abs(0.5f - phase) * 2f
        val gx = w * (0.16f + 0.68f * sweep)
        val gy = h * (0.62f - 0.30f * sweep)
        canvas.save()
        canvas.translate(gx, gy)
        canvas.drawRect(-glowRadius, -glowRadius, glowRadius, glowRadius, glowPaint)
        canvas.restore()
    }

    /** Interpolates channel [slot] (0 top, 1 mid, 2 bottom) at a fractional hour. */
    private fun skyColour(hour: Float, slot: Int, dim: Float): Int {
        var index = 0
        while (index < SKY.size - 2 && hour >= SKY[index + 1].hour) index++
        val from = SKY[index]
        val to = SKY[index + 1]
        val span = to.hour - from.hour
        val t = if (span <= 0f) 0f else ((hour - from.hour) / span).coerceIn(0f, 1f)
        return scale(lerpColour(from.colour(slot), to.colour(slot), t), dim)
    }

    // --- clock --------------------------------------------------------------

    private fun drawClock(canvas: Canvas, w: Float, h: Float, now: Date) {
        timePaint.textSize = h * 0.34f
        meridiemPaint.textSize = h * 0.085f
        datePaint.textSize = h * 0.078f

        applyDim(timePaint, 0xFFFFFF)
        applyDim(meridiemPaint, 0xC7D0DE, 210)
        applyDim(datePaint, 0xA8B3C4, 235)

        val timeText = timeFormat.format(now)
        val meridiem = meridiemFormat.format(now).uppercase(Locale.getDefault())

        // Centre the time and its AM/PM as one block, so 9:05 and 12:45 are not
        // nudged off-centre by the suffix.
        val timeWidth = timePaint.measureText(timeText)
        val gap = h * 0.022f
        val meridiemWidth = meridiemPaint.measureText(meridiem)
        val startX = (w - (timeWidth + gap + meridiemWidth)) / 2f
        val baseline = h * 0.50f

        canvas.drawText(timeText, startX, baseline, timePaint)
        canvas.drawText(
            meridiem,
            startX + timeWidth + gap,
            baseline - timePaint.textSize * 0.52f,
            meridiemPaint
        )
        canvas.drawText(dateFormat.format(now), w / 2f, h * 0.635f, datePaint)
    }

    // --- weather ------------------------------------------------------------

    /**
     * A translucent card in the bottom-left corner: glyph, temperature,
     * condition. Sized from its own contents so a long condition ("Freezing
     * drizzle") widens the card instead of being clipped.
     *
     * Everything is drawn in code -- no icon library, no drawable resources,
     * nothing to keep in sync with a density bucket.
     */
    private fun drawWeatherCard(canvas: Canvas, w: Float, h: Float) {
        val reading = weather
        val cardH = h * 0.19f
        val pad = cardH * 0.20f
        val glyphRadius = cardH * 0.26f

        tempPaint.textSize = cardH * 0.46f
        conditionPaint.textSize = cardH * 0.24f

        val tempText = reading?.let { formatTemperature(it.tempC) } ?: PLACEHOLDER_TEMP
        val conditionText = reading?.label ?: PLACEHOLDER_CONDITION

        val textLeft = pad + glyphRadius * 2f + pad * 0.7f
        val contentWidth = max(
            tempPaint.measureText(tempText),
            conditionPaint.measureText(conditionText)
        )
        val cardW = textLeft + contentWidth + pad

        val left = w * 0.035f
        val bottom = h * 0.94f
        val top = bottom - cardH
        rect.set(left, top, left + cardW, bottom)

        // ~7% white fill and a hairline stroke: enough to separate the card
        // from the sky at a glance, not enough to become a bright rectangle on
        // a display that is on all night.
        cardFillPaint.color = whiteAlpha(0.075f)
        canvas.drawRoundRect(rect, cardH * 0.22f, cardH * 0.22f, cardFillPaint)
        cardStrokePaint.color = whiteAlpha(0.14f)
        cardStrokePaint.strokeWidth = max(1f, h * 0.0025f)
        canvas.drawRoundRect(rect, cardH * 0.22f, cardH * 0.22f, cardStrokePaint)

        val glyphX = left + pad + glyphRadius
        val glyphY = top + cardH * 0.5f
        val faded = reading == null
        applyDim(glyphFillPaint, if (faded) 0x8894A6 else 0xE8EEF7, if (faded) 150 else 240)
        applyDim(glyphStrokePaint, if (faded) 0x8894A6 else 0xE8EEF7, if (faded) 150 else 240)
        glyphStrokePaint.strokeWidth = glyphRadius * 0.16f
        drawGlyph(
            canvas,
            reading?.condition ?: Protocol.Condition.CLOUD,
            reading?.isDay ?: true,
            glyphX,
            glyphY,
            glyphRadius
        )

        applyDim(tempPaint, 0xFFFFFF, if (faded) 170 else 245)
        applyDim(conditionPaint, 0x9FAABC, if (faded) 150 else 225)
        canvas.drawText(tempText, left + textLeft, top + cardH * 0.53f, tempPaint)
        canvas.drawText(conditionText, left + textLeft, top + cardH * 0.83f, conditionPaint)
    }

    /**
     * Fahrenheit, because the default location is Syracuse NY and a household
     * there does not read 23 degrees as warm. The wire format stays Celsius --
     * `temp_c` is the protocol's field and the conversion belongs here, at the
     * one place that knows who is reading the screen.
     */
    private fun formatTemperature(celsius: Double): String {
        val shown = if (FAHRENHEIT) celsius * 9.0 / 5.0 + 32.0 else celsius
        return "${shown.roundToInt()}°"
    }

    private fun drawGlyph(
        canvas: Canvas,
        condition: Protocol.Condition,
        isDay: Boolean,
        cx: Float,
        cy: Float,
        r: Float
    ) {
        if (glyphPathRadius != r) rebuildGlyphPaths(r)

        when (condition) {
            Protocol.Condition.CLEAR ->
                if (isDay) drawSun(canvas, cx, cy, r) else drawMoon(canvas, cx, cy)

            Protocol.Condition.PARTLY -> {
                // The luminary peeks out of the top-left of the cloud.
                val lx = cx - r * 0.30f
                val ly = cy - r * 0.34f
                if (isDay) drawSun(canvas, lx, ly, r * 0.62f) else drawMoon(canvas, lx, ly)
                drawCloud(canvas, cx + r * 0.12f, cy + r * 0.22f, r * 0.86f)
            }

            Protocol.Condition.CLOUD -> drawCloud(canvas, cx, cy, r)

            Protocol.Condition.FOG -> {
                drawCloud(canvas, cx, cy - r * 0.22f, r * 0.82f)
                for (i in 0..1) {
                    val y = cy + r * (0.60f + 0.30f * i)
                    canvas.drawLine(
                        cx - r * (0.62f - 0.12f * i), y,
                        cx + r * (0.62f - 0.12f * i), y,
                        glyphStrokePaint
                    )
                }
            }

            Protocol.Condition.RAIN -> {
                drawCloud(canvas, cx, cy - r * 0.24f, r * 0.86f)
                for (i in 0..2) {
                    val x = cx + r * (-0.42f + 0.42f * i)
                    canvas.drawLine(
                        x + r * 0.10f, cy + r * 0.44f,
                        x - r * 0.06f, cy + r * 0.86f,
                        glyphStrokePaint
                    )
                }
            }

            Protocol.Condition.SNOW -> {
                drawCloud(canvas, cx, cy - r * 0.24f, r * 0.86f)
                for (i in 0..2) {
                    val x = cx + r * (-0.42f + 0.42f * i)
                    val y = cy + r * (if (i == 1) 0.78f else 0.60f)
                    canvas.drawCircle(x, y, r * 0.11f, glyphFillPaint)
                }
            }

            Protocol.Condition.STORM -> {
                drawCloud(canvas, cx, cy - r * 0.26f, r * 0.86f)
                canvas.save()
                canvas.translate(cx, cy + r * 0.30f)
                canvas.drawPath(boltPath, glyphFillPaint)
                canvas.restore()
            }
        }
    }

    private fun drawSun(canvas: Canvas, cx: Float, cy: Float, r: Float) {
        canvas.drawCircle(cx, cy, r * 0.50f, glyphFillPaint)
        val saved = glyphStrokePaint.strokeWidth
        glyphStrokePaint.strokeWidth = r * 0.15f
        for (i in 0 until 8) {
            val angle = Math.toRadians(i * 45.0)
            val dx = Math.cos(angle).toFloat()
            val dy = Math.sin(angle).toFloat()
            canvas.drawLine(
                cx + dx * r * 0.70f, cy + dy * r * 0.70f,
                cx + dx * r * 0.98f, cy + dy * r * 0.98f,
                glyphStrokePaint
            )
        }
        glyphStrokePaint.strokeWidth = saved
    }

    /** The crescent is a real boolean subtraction, cached in [moonPath]. */
    private fun drawMoon(canvas: Canvas, cx: Float, cy: Float) {
        canvas.save()
        canvas.translate(cx, cy)
        canvas.drawPath(moonPath, glyphFillPaint)
        canvas.restore()
    }

    private fun drawCloud(canvas: Canvas, cx: Float, cy: Float, r: Float) {
        canvas.drawCircle(cx - r * 0.38f, cy + r * 0.10f, r * 0.34f, glyphFillPaint)
        canvas.drawCircle(cx + r * 0.02f, cy - r * 0.16f, r * 0.44f, glyphFillPaint)
        canvas.drawCircle(cx + r * 0.44f, cy + r * 0.12f, r * 0.32f, glyphFillPaint)
        rect.set(cx - r * 0.72f, cy + r * 0.06f, cx + r * 0.76f, cy + r * 0.44f)
        canvas.drawRoundRect(rect, r * 0.19f, r * 0.19f, glyphFillPaint)
    }

    /**
     * Paths that cannot be expressed as primitives, rebuilt only when the glyph
     * size changes -- which is once, at the first layout.
     */
    private fun rebuildGlyphPaths(r: Float) {
        glyphPathRadius = r

        moonOuter.reset()
        moonOuter.addCircle(0f, 0f, r * 0.62f, Path.Direction.CW)
        moonInner.reset()
        moonInner.addCircle(r * 0.42f, -r * 0.26f, r * 0.58f, Path.Direction.CW)
        moonPath.reset()
        moonPath.op(moonOuter, moonInner, Path.Op.DIFFERENCE)

        boltPath.reset()
        boltPath.moveTo(r * 0.16f, -r * 0.10f)
        boltPath.lineTo(-r * 0.20f, r * 0.34f)
        boltPath.lineTo(-r * 0.01f, r * 0.34f)
        boltPath.lineTo(-r * 0.16f, r * 0.72f)
        boltPath.lineTo(r * 0.22f, r * 0.24f)
        boltPath.lineTo(r * 0.02f, r * 0.24f)
        boltPath.close()
    }

    // --- link indicator -----------------------------------------------------

    /**
     * A small glowing dot on the bottom edge. Deliberately subtle: the link
     * being down is not an error the household needs shouting at them, and the
     * home screen is fully functional either way. The word only appears when
     * something is actually wrong, because a permanent "ONLINE" badge is noise
     * 99% of the time.
     */
    private fun drawLinkDot(canvas: Canvas, w: Float, h: Float) {
        val rgb = when (indicator) {
            LinkIndicator.ONLINE -> 0x5CC98A
            LinkIndicator.CONNECTING -> 0xD2A44A
            LinkIndicator.OFFLINE -> 0x5A6474
        }
        val label = when (indicator) {
            LinkIndicator.ONLINE -> null
            LinkIndicator.CONNECTING -> "CONNECTING"
            LinkIndicator.OFFLINE -> "OFFLINE"
        }

        val radius = h * 0.0145f
        offlinePaint.textSize = h * 0.038f
        applyDim(offlinePaint, rgb, 165)
        val labelWidth = label?.let { offlinePaint.measureText(it) } ?: 0f
        val gap = if (label == null) 0f else radius * 2.2f
        val cx = (w - (radius * 2f + gap + labelWidth)) / 2f + radius
        // On the same line as the bottom of the weather card and the tiles, so
        // the glow has room to fall off instead of being cut by the panel edge.
        val cy = h * 0.925f

        // The glow is three concentric circles rather than a shadow layer:
        // Paint.setShadowLayer is only dependable for text on a hardware
        // canvas, and three drawCircle calls cost less than a saveLayer.
        applyDim(dotPaint, rgb, 34)
        canvas.drawCircle(cx, cy, radius * 3.1f, dotPaint)
        applyDim(dotPaint, rgb, 70)
        canvas.drawCircle(cx, cy, radius * 1.9f, dotPaint)
        applyDim(dotPaint, rgb, 255)
        canvas.drawCircle(cx, cy, radius, dotPaint)

        if (label != null) {
            canvas.drawText(label, cx + radius + gap, cy + offlinePaint.textSize * 0.36f, offlinePaint)
        }
    }

    // --- colour helpers -----------------------------------------------------

    private fun applyDim(paint: Paint, rgb: Int, alpha: Int = 255) {
        paint.color = Color.argb(
            alpha,
            ((rgb shr 16 and 0xFF) * dimFactor).toInt().coerceIn(0, 255),
            ((rgb shr 8 and 0xFF) * dimFactor).toInt().coerceIn(0, 255),
            ((rgb and 0xFF) * dimFactor).toInt().coerceIn(0, 255)
        )
    }

    private fun whiteAlpha(fraction: Float): Int =
        Color.argb((255 * fraction * dimFactor).toInt().coerceIn(0, 255), 255, 255, 255)

    companion object {
        private const val MIN_DIM = 0.22f

        /** The default location is Syracuse NY; the wire stays Celsius. */
        private const val FAHRENHEIT = true

        /** Reads as a temperature that has not arrived, not as a broken card. */
        private const val PLACEHOLDER_TEMP = "–°"
        private const val PLACEHOLDER_CONDITION = "No weather yet"

        /** One full left-to-right-and-back sweep of the glow. */
        private const val DRIFT_PERIOD_MS = 7L * 60L * 1000L
        private const val GLOW_ALPHA = 52

        /**
         * Time-of-day palette, as (hour, top, mid, bottom) keyframes that the
         * renderer interpolates between. The flat stretches (00:00-06:30 and
         * 09:00-17:00) are deliberate: the sky should be still most of the
         * time and only move around dawn and dusk.
         */
        private class Sky(
            val hour: Float,
            val top: Int,
            val mid: Int,
            val bottom: Int
        ) {
            fun colour(slot: Int): Int = when (slot) {
                0 -> top
                1 -> mid
                else -> bottom
            }
        }

        private val SKY = arrayOf(
            //                          top       mid       bottom
            Sky(0f, /*    night */ 0x0A0E1F, 0x0D1326, 0x131A33),
            Sky(6.5f, /*  night */ 0x0A0E1F, 0x0D1326, 0x131A33),
            Sky(7.75f, /* dawn  */ 0x1A1A33, 0x2A2342, 0x4A3B52),
            Sky(9f, /*    day   */ 0x1C2740, 0x202D4C, 0x24365C),
            Sky(17f, /*   day   */ 0x1C2740, 0x202D4C, 0x24365C),
            Sky(18.75f, /* dusk */ 0x24365C, 0x3A2C4A, 0x5A3A3A),
            Sky(20.5f, /* night */ 0x0A0E1F, 0x0D1326, 0x131A33),
            Sky(24f, /*   wrap  */ 0x0A0E1F, 0x0D1326, 0x131A33)
        )

        private fun lerpColour(from: Int, to: Int, t: Float): Int {
            val r = (from shr 16 and 0xFF) + ((to shr 16 and 0xFF) - (from shr 16 and 0xFF)) * t
            val g = (from shr 8 and 0xFF) + ((to shr 8 and 0xFF) - (from shr 8 and 0xFF)) * t
            val b = (from and 0xFF) + ((to and 0xFF) - (from and 0xFF)) * t
            return (r.toInt() shl 16) or (g.toInt() shl 8) or b.toInt()
        }

        private fun scale(rgb: Int, factor: Float): Int = Color.rgb(
            ((rgb shr 16 and 0xFF) * factor).toInt().coerceIn(0, 255),
            ((rgb shr 8 and 0xFF) * factor).toInt().coerceIn(0, 255),
            ((rgb and 0xFF) * factor).toInt().coerceIn(0, 255)
        )

        private fun lighten(argb: Int, towardsWhite: Float, alpha: Int): Int = Color.argb(
            alpha,
            lift(Color.red(argb), towardsWhite),
            lift(Color.green(argb), towardsWhite),
            lift(Color.blue(argb), towardsWhite)
        )

        private fun lift(channel: Int, t: Float): Int =
            (channel + (255 - channel) * t).toInt().coerceIn(0, 255)

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
