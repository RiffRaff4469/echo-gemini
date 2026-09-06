package com.echogemini.terminal

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.view.View
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * Rasterises the ambient home screen at the panel's real size.
 *
 * The screen is drawn entirely in [AmbientClock.onDraw] -- shaders, a boolean
 * path subtraction for the crescent moon, seven hand-drawn glyphs -- none of
 * which the compiler can check. This renders it for real (Robolectric's native
 * graphics mode) so a null payload, an unknown WMO code or a glyph branch that
 * throws is caught here rather than on a wall in a kitchen.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [30])
class AmbientClockRenderTest {

    /** The Show's panel. Every dimension in the view is a fraction of these. */
    private val panelWidth = 960
    private val panelHeight = 480

    private fun render(configure: (AmbientClock) -> Unit = {}): Bitmap {
        val view = AmbientClock(RuntimeEnvironment.getApplication())
        configure(view)
        view.measure(
            View.MeasureSpec.makeMeasureSpec(panelWidth, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(panelHeight, View.MeasureSpec.EXACTLY)
        )
        view.layout(0, 0, panelWidth, panelHeight)
        val bitmap = Bitmap.createBitmap(panelWidth, panelHeight, Bitmap.Config.ARGB_8888)
        view.draw(Canvas(bitmap))
        return bitmap
    }

    @Test
    fun `the backdrop is a gradient, not the black canvas it replaced`() {
        val bitmap = render()
        val top = bitmap.getPixel(panelWidth / 2, 4)
        val bottom = bitmap.getPixel(panelWidth / 2, panelHeight - 4)
        assertTrue("the sky should never be pure black", Color.blue(top) > 0)
        assertTrue(
            "top and bottom of the gradient should differ",
            Color.blue(top) != Color.blue(bottom) || Color.red(top) != Color.red(bottom)
        )
        // Dark-first: this panel is on all night and must not glow white.
        assertTrue("the sky must stay dark", Color.red(bottom) < 140)
    }

    @Test
    fun `the clock renders with no weather ever pushed`() {
        // The normal state with the PC switched off, and it must never be the
        // state that crashes the one screen that is meant to survive it.
        val bitmap = render { it.weather = null }
        assertTrue("the time should be legible on the sky", hasBrightPixels(bitmap))
    }

    @Test
    fun `every condition glyph draws, day and night`() {
        for (code in WMO_CODES) {
            for (isDay in listOf(true, false)) {
                val bitmap = render {
                    it.weather = Protocol.Weather(tempC = -4.5, code = code, isDay = isDay)
                }
                assertTrue("code $code (day=$isDay) drew nothing", hasBrightPixels(bitmap))
            }
        }
    }

    @Test
    fun `an unknown weather code falls back to cloud rather than failing`() {
        assertEquals(Protocol.Condition.CLOUD, Protocol.Weather.conditionFor(1234))
        val bitmap = render {
            it.weather = Protocol.Weather(tempC = 9.0, code = 1234, isDay = true)
        }
        assertTrue("an unknown code drew nothing", hasBrightPixels(bitmap))
    }

    @Test
    fun `each link state draws its dot`() {
        for (state in AmbientClock.LinkIndicator.values()) {
            assertTrue("$state drew nothing", hasBrightPixels(render { it.indicator = state }))
        }
    }

    @Test
    fun `a fully dimmed screen still draws and is darker than a bright one`() {
        val bright = averageLuminance(render { it.dimFactor = 1f })
        val dim = averageLuminance(render { it.dimFactor = 0f })  // clamped to MIN_DIM
        assertTrue("dimming should darken the whole screen ($bright -> $dim)", dim < bright)
    }

    // --- helpers ------------------------------------------------------------

    /** True if anything was drawn appreciably lighter than the backdrop. */
    private fun hasBrightPixels(bitmap: Bitmap): Boolean {
        for (y in 0 until bitmap.height step 3) {
            for (x in 0 until bitmap.width step 3) {
                val pixel = bitmap.getPixel(x, y)
                if (Color.red(pixel) > 170 && Color.green(pixel) > 170) return true
            }
        }
        return false
    }

    private fun averageLuminance(bitmap: Bitmap): Double {
        var total = 0.0
        var count = 0
        for (y in 0 until bitmap.height step 4) {
            for (x in 0 until bitmap.width step 4) {
                val pixel = bitmap.getPixel(x, y)
                total += 0.299 * Color.red(pixel) +
                    0.587 * Color.green(pixel) +
                    0.114 * Color.blue(pixel)
                count++
            }
        }
        return total / count
    }

    private companion object {
        /** One representative code from each WMO group the client maps. */
        val WMO_CODES = listOf(0, 1, 2, 3, 45, 51, 61, 66, 71, 77, 80, 85, 95, 96)
    }
}
