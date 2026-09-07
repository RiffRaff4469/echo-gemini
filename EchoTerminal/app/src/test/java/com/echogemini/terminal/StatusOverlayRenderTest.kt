package com.echogemini.terminal

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.os.Looper
import android.view.View
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * Rasterises the status band at the size [MainActivity] gives it.
 *
 * Same reasoning as [AmbientClockRenderTest]: this band is drawn by hand onto a
 * [Canvas], so nothing about where the marks land is checked by the compiler.
 * The specific worry for the post-answer hint is collision -- it is new text on
 * a band that already carries the camera indicator, and HANDOFF section 8.3
 * makes "the camera is visibly on" a product requirement, not a nicety.
 */
@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [30])
class StatusOverlayRenderTest {

    /** MainActivity gives the overlay the full panel width and a tenth of its height. */
    private val bandWidth = 960
    private val bandHeight = 48

    private fun render(configure: (StatusOverlay) -> Unit = {}): Bitmap {
        val view = StatusOverlay(RuntimeEnvironment.getApplication())
        configure(view)
        // The hint's alpha is driven by a posted Runnable, and Robolectric's
        // main looper is paused until asked.
        shadowOf(Looper.getMainLooper()).idle()
        view.measure(
            View.MeasureSpec.makeMeasureSpec(bandWidth, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(bandHeight, View.MeasureSpec.EXACTLY)
        )
        view.layout(0, 0, bandWidth, bandHeight)
        val bitmap = Bitmap.createBitmap(bandWidth, bandHeight, Bitmap.Config.ARGB_8888)
        view.draw(Canvas(bitmap))
        return bitmap
    }

    @Test
    fun `an idle band with no window draws nothing at all`() {
        // The ambient home screen owns the display. Idle means idle.
        assertEquals(0, drawnColumns(render()).size)
    }

    @Test
    fun `every conversation state draws its pill`() {
        for (state in Protocol.UiState.values()) {
            val drawn = drawnColumns(render { it.state = state }).size
            if (state == Protocol.UiState.IDLE) {
                assertEquals("IDLE must stay blank", 0, drawn)
            } else {
                assertTrue("$state drew nothing", drawn > 0)
            }
        }
    }

    @Test
    fun `the stop hint appears only once a window is armed`() {
        val quiet = render { it.state = Protocol.UiState.LISTENING }
        val armed = render {
            it.state = Protocol.UiState.LISTENING
            // Well inside HINT_LEAD_MS, so the hint is fully faded in.
            it.setQuietWindow(500)
        }
        assertTrue(
            "the hint must add marks to the band",
            rightmostDrawn(armed) > rightmostDrawn(quiet)
        )
    }

    @Test
    fun `a cancelled window takes the hint away again`() {
        val armed = render {
            it.state = Protocol.UiState.LISTENING
            it.setQuietWindow(500)
        }
        val cancelled = render {
            it.state = Protocol.UiState.LISTENING
            it.setQuietWindow(500)
            it.clearQuietWindow()
        }
        assertTrue(
            "speaking again must retract the hint, not leave it counting down",
            rightmostDrawn(cancelled) < rightmostDrawn(armed)
        )
    }

    @Test
    fun `the hint never reaches the camera indicator`() {
        val armed = render {
            it.state = Protocol.UiState.LISTENING
            it.setQuietWindow(500)
        }
        val camera = render {
            it.state = Protocol.UiState.LISTENING
            it.cameraStreaming = true
        }
        val hintEnds = rightmostDrawn(armed)
        val pillStarts = leftmostDrawn(camera, from = bandWidth / 2)
        assertTrue(
            "the hint runs to $hintEnds, the CAMERA ON pill starts at $pillStarts",
            hintEnds < pillStarts
        )
    }

    @Test
    fun `the hint sits beside the state pill in every state`() {
        // The hint is positioned from the pill's right edge, so a state whose
        // pill is a different width must not push it off the band.
        for (state in Protocol.UiState.values()) {
            val bitmap = render {
                it.state = state
                it.setQuietWindow(500)
            }
            assertTrue("$state lost the hint", drawnColumns(bitmap).isNotEmpty())
            assertTrue("$state pushed the hint off the band", rightmostDrawn(bitmap) < bandWidth)
        }
    }

    // --- helpers ------------------------------------------------------------

    /** Columns holding anything at all. The view has no background, so
     *  everything not drawn stays fully transparent. */
    private fun drawnColumns(bitmap: Bitmap): List<Int> = buildList {
        for (x in 0 until bitmap.width) {
            for (y in 0 until bitmap.height) {
                if (Color.alpha(bitmap.getPixel(x, y)) > 8) {
                    add(x)
                    break
                }
            }
        }
    }

    private fun rightmostDrawn(bitmap: Bitmap): Int = drawnColumns(bitmap).lastOrNull() ?: -1

    private fun leftmostDrawn(bitmap: Bitmap, from: Int): Int =
        drawnColumns(bitmap).firstOrNull { it >= from } ?: bitmap.width
}
