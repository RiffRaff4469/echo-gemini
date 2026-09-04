package com.echogemini.terminal

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.SurfaceTexture
import android.graphics.YuvImage
import android.hardware.Camera
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import java.io.ByteArrayOutputStream

/**
 * The single owner of the camera. Read HANDOFF sections 2, 5.9 and 8.3 before
 * changing anything in this file.
 *
 * ## The constraints this class exists to satisfy
 *
 * 1. **Legacy HAL1 means single-client access. One consumer at a time, ever.**
 *    Enforced structurally: there is one instance, every camera operation is
 *    posted to one dedicated [HandlerThread], and [open]/[release] are
 *    idempotent. No other class in this app touches [Camera].
 *
 * 2. **Never SIGKILL or force-stop `cameraserver`.** Doing so while streaming
 *    causes an IOMMU livelock that requires physically power-cycling the
 *    device. There is deliberately no `Process.killProcess`, no `am force-stop`,
 *    no `pkill`, and no shell escape anywhere in this file, and there must
 *    never be. This is the highest-consequence software failure in the project.
 *
 * 3. **No aggressive recovery.** On a camera error we release, report upward,
 *    and STOP. We do not reopen, we do not retry in a loop. A retry loop
 *    against a wedged HAL is precisely how (2) happens. [consecutiveErrors]
 *    latches the source off until the server explicitly asks again.
 *
 * 4. **Open on demand, release the moment the session ends.** The camera opens
 *    only when the server sends `video enabled`, never at boot, never
 *    speculatively, never to "warm up".
 *
 * 5. **The physical privacy shutter is handled, not ignored.** Kernel patches
 *    0015/0016 keep the camera enumerated when the latch is engaged, so frames
 *    come back essentially black rather than erroring. Black frames are
 *    detected and reported as [Protocol.CameraStatus.SHUTTER_CLOSED] rather
 *    than being silently streamed to a cloud model.
 *
 * ## Why the deprecated `android.hardware.Camera` API
 *
 * The device is HAL1. Camera2 works there only through the LEGACY shim, which
 * translates every request back into exactly these calls with extra machinery
 * in between -- on a build whose camera stack is already held together by
 * out-of-tree patches and an `LD_PRELOAD` shim. The old API maps directly onto
 * what the hardware actually does. Do not "modernise" this to Camera2.
 */
class CameraSource(private val callbacks: Callbacks) {

    interface Callbacks {
        /** A ready-to-send JPEG frame. */
        fun onFrame(jpeg: ByteArray)

        /** Every state change, mirrored to the server and the on-screen indicator. */
        fun onStatus(status: Protocol.CameraStatus, detail: String)
    }

    /** Single lock guarding every transition. */
    private val lock = Any()

    private val thread = HandlerThread("echo-camera").apply { start() }
    private val handler = Handler(thread.looper)

    @Volatile
    private var camera: Camera? = null

    @Volatile
    private var surface: SurfaceTexture? = null

    @Volatile
    var isOpen: Boolean = false
        private set

    /**
     * Latches after a failure. While non-zero the source refuses to open again
     * without an explicit new request -- see constraint (3).
     */
    @Volatile
    private var consecutiveErrors = 0

    private var previewWidth = 0
    private var previewHeight = 0
    private var minFrameIntervalMs = 1000L
    private var jpegQuality = 80

    private var lastFrameAt = 0L
    private var lastShutterReportAt = 0L
    private var encoding = false
    private var framesSent = 0L

    // --- lifecycle ----------------------------------------------------------

    /**
     * Open the camera and start delivering frames. Safe to call repeatedly;
     * a second call while open only updates the rate and quality.
     *
     * @param fps clamped to the API's 1 FPS ceiling -- extra frames are wasted
     *   work on the device and are discarded by the API anyway.
     */
    fun open(fps: Double, quality: Int) {
        handler.post {
            synchronized(lock) {
                minFrameIntervalMs = (1000.0 / fps.coerceIn(0.05, Protocol.VIDEO_MAX_FPS)).toLong()
                jpegQuality = quality.coerceIn(30, 95)

                if (isOpen) {
                    Log.i(TAG, "already open; rate now ${minFrameIntervalMs}ms q=$jpegQuality")
                    return@post
                }
                if (consecutiveErrors > 0) {
                    // The server asked again after a failure, which is the only
                    // thing that clears the latch. Never self-clearing.
                    Log.i(TAG, "clearing error latch on explicit request")
                    consecutiveErrors = 0
                }
                openLocked()
            }
        }
    }

    /**
     * Release the camera. Called when the session ends for ANY reason -- idle
     * close, error, or the device link dropping. Leaving a HAL1 camera open is
     * how this hardware wedges.
     */
    fun release() {
        handler.post {
            synchronized(lock) { releaseLocked("requested") }
        }
    }

    /** Tear down permanently, on activity destroy. */
    fun shutdown() {
        handler.post {
            synchronized(lock) { releaseLocked("shutdown") }
        }
        thread.quitSafely()
    }

    // --- open / close, always on the camera thread --------------------------

    private fun openLocked() {
        callbacks.onStatus(Protocol.CameraStatus.OPENING, "")

        val cameraId = findCameraId()
        if (cameraId < 0) {
            fail("no camera device enumerated -- has the §5.7 shim step been re-run after the last ROM flash?")
            return
        }

        val opened = try {
            Camera.open(cameraId)
        } catch (e: RuntimeException) {
            // "Fail to connect to camera service" lands here. Another client
            // has it, or the HAL is unhappy. Either way: do not retry.
            fail("Camera.open failed: ${e.message}")
            return
        }

        if (opened == null) {
            fail("Camera.open returned null for id $cameraId")
            return
        }

        camera = opened
        try {
            configure(opened)

            // A detached SurfaceTexture: the camera needs a preview target, but
            // nothing is drawn from it. The frames we want arrive through the
            // preview callback, and a visible preview would cost RAM and GPU
            // for no benefit -- the on-screen indicator is a separate, simpler
            // view.
            val texture = SurfaceTexture(false)
            surface = texture
            opened.setPreviewTexture(texture)

            opened.setErrorCallback { error, _ ->
                // Note what is NOT here: no restart, no reopen, no killing
                // anything. Release and surface it.
                val detail = when (error) {
                    Camera.CAMERA_ERROR_SERVER_DIED -> "media server died"
                    Camera.CAMERA_ERROR_EVICTED -> "camera evicted by another client"
                    else -> "camera error $error"
                }
                Log.e(TAG, "HAL reported: $detail")
                handler.post { synchronized(lock) { fail(detail) } }
            }

            // WithBuffer + a preallocated buffer: on a 1 GB device, allocating a
            // ~1.4 MB NV21 array per preview frame would keep the GC permanently
            // busy for frames we mostly throw away.
            val frameBytes = previewWidth * previewHeight *
                ImageFormat.getBitsPerPixel(ImageFormat.NV21) / 8
            opened.addCallbackBuffer(ByteArray(frameBytes))
            opened.addCallbackBuffer(ByteArray(frameBytes))
            opened.setPreviewCallbackWithBuffer(previewCallback)

            opened.startPreview()
        } catch (e: Exception) {
            fail("could not start preview: ${e.message}")
            return
        }

        isOpen = true
        lastFrameAt = 0L
        framesSent = 0L
        Log.i(
            TAG,
            "camera open: preview ${previewWidth}x$previewHeight, " +
                "1 frame per ${minFrameIntervalMs}ms, jpeg q=$jpegQuality"
        )
        callbacks.onStatus(Protocol.CameraStatus.STREAMING, "")
    }

    private fun configure(cam: Camera) {
        val params = cam.parameters
        params.previewFormat = ImageFormat.NV21

        val size = chooseSize(params.supportedPreviewSizes)
        previewWidth = size.width
        previewHeight = size.height
        params.setPreviewSize(previewWidth, previewHeight)

        // Auto-exposure and adaptive white balance are what the §5 patches
        // deliver; ask for them rather than pinning anything manually.
        params.supportedFocusModes
            ?.firstOrNull { it == Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE }
            ?.let { params.focusMode = it }
        params.supportedWhiteBalance
            ?.firstOrNull { it == Camera.Parameters.WHITE_BALANCE_AUTO }
            ?.let { params.whiteBalance = it }
        params.supportedAntibanding
            ?.firstOrNull { it == Camera.Parameters.ANTIBANDING_AUTO }
            ?.let { params.antibanding = it }

        // Patch 0014 fixes the OV9734 vertical flip in the kernel and §5.8
        // expects `Orientation: 0`, so no rotation is applied here. If frames
        // arrive upside down, the ROM is wrong -- fix it there, not here.
        cam.parameters = params
    }

    /**
     * Prefer the smallest preview size whose short edge still reaches 768, so
     * the downscale to the target is real rather than an upscale. Native still
     * capture is 1280x720, so in practice the short edge is 720 and frames go
     * out at 720x720 -- inside the API's spec, which recommends 768x768 rather
     * than requiring it. Upscaling to hit 768 exactly would invent detail.
     */
    private fun chooseSize(sizes: List<Camera.Size>?): Camera.Size {
        val available = sizes?.takeIf { it.isNotEmpty() }
            ?: throw IllegalStateException("HAL reported no supported preview sizes")
        val bigEnough = available.filter {
            minOf(it.width, it.height) >= Protocol.VIDEO_EDGE
        }
        return if (bigEnough.isNotEmpty()) {
            bigEnough.minByOrNull { it.width * it.height }!!
        } else {
            available.maxByOrNull { minOf(it.width, it.height) }!!
        }
    }

    private fun findCameraId(): Int {
        val count = try {
            Camera.getNumberOfCameras()
        } catch (e: RuntimeException) {
            Log.e(TAG, "getNumberOfCameras threw", e)
            return -1
        }
        if (count <= 0) return -1

        val info = Camera.CameraInfo()
        for (id in 0 until count) {
            Camera.getCameraInfo(id, info)
            if (info.facing == Camera.CameraInfo.CAMERA_FACING_FRONT) return id
        }
        return 0  // the Show only has the one, but do not assume the index
    }

    private fun releaseLocked(reason: String) {
        val cam = camera
        isOpen = false
        camera = null

        if (cam != null) {
            // Order matters: stop callbacks, stop preview, THEN release. Calling
            // release() with the preview still running is one of the ways to
            // leave the HAL in a bad state.
            try {
                cam.setPreviewCallbackWithBuffer(null)
                cam.setErrorCallback(null)
                cam.stopPreview()
            } catch (e: Exception) {
                Log.w(TAG, "stopPreview threw during release", e)
            }
            try {
                cam.release()
            } catch (e: Exception) {
                Log.w(TAG, "release threw", e)
            }
            Log.i(TAG, "camera released ($reason) after $framesSent frames")
        }

        surface?.release()
        surface = null

        if (reason != "shutdown") {
            callbacks.onStatus(Protocol.CameraStatus.RELEASED, reason)
        }
    }

    private fun fail(detail: String) {
        consecutiveErrors++
        Log.e(TAG, "camera failure ($consecutiveErrors): $detail")
        releaseLocked("error")
        // Back off and surface. No reopen -- see constraint (3).
        callbacks.onStatus(Protocol.CameraStatus.ERROR, detail)
    }

    // --- frames -------------------------------------------------------------

    private val previewCallback = Camera.PreviewCallback { data, cam ->
        if (data == null) return@PreviewCallback

        val now = SystemClock.elapsedRealtime()
        val due = now - lastFrameAt >= minFrameIntervalMs

        // Hand the buffer straight back unless we are actually going to use it;
        // holding buffers starves the preview pipeline.
        if (!due || encoding || !isOpen) {
            cam.addCallbackBuffer(data)
            return@PreviewCallback
        }

        if (isNearBlack(data, previewWidth * previewHeight)) {
            reportShutter(now)
            lastFrameAt = now
            cam.addCallbackBuffer(data)
            return@PreviewCallback
        }

        lastFrameAt = now
        encoding = true
        val copy = data.copyOf()          // release the HAL's buffer immediately
        cam.addCallbackBuffer(data)

        try {
            val jpeg = encode(copy)
            if (jpeg != null) {
                framesSent++
                callbacks.onFrame(jpeg)
            }
        } catch (e: Throwable) {
            // OutOfMemory is a real possibility on 1 GB. Drop the frame; do not
            // let it propagate up the camera thread and kill the source.
            Log.w(TAG, "frame encode failed; dropping", e)
        } finally {
            encoding = false
        }
    }

    /**
     * Detects the physical privacy shutter.
     *
     * With the latch engaged the camera stays enumerated (patches 0015/0016) and
     * simply returns an essentially black image. Averaging the NV21 luma plane
     * is enough to tell that apart from a dark room, because a dark room still
     * has some variation and some pixels well above the floor.
     *
     * Sampled rather than summed in full: 720x720 is half a million bytes and
     * this runs on the camera thread.
     */
    private fun isNearBlack(nv21: ByteArray, lumaBytes: Int): Boolean {
        val limit = minOf(lumaBytes, nv21.size)
        if (limit <= 0) return false

        var sum = 0L
        var samples = 0
        var brightest = 0
        var i = 0
        while (i < limit) {
            val y = nv21[i].toInt() and 0xFF
            sum += y
            if (y > brightest) brightest = y
            samples++
            i += LUMA_SAMPLE_STRIDE
        }
        if (samples == 0) return false

        val mean = sum.toDouble() / samples
        // Both conditions: a genuinely dark room is dim on average but still has
        // highlights somewhere. A closed shutter has neither.
        return mean < NEAR_BLACK_MEAN && brightest < NEAR_BLACK_PEAK
    }

    private fun reportShutter(now: Long) {
        if (now - lastShutterReportAt < SHUTTER_REPORT_INTERVAL_MS) return
        lastShutterReportAt = now
        Log.i(TAG, "frames are near-black; reporting the privacy shutter as closed")
        callbacks.onStatus(
            Protocol.CameraStatus.SHUTTER_CLOSED,
            "frames are near-black; the physical privacy shutter appears to be closed"
        )
    }

    /** NV21 preview frame -> centre-cropped, downscaled JPEG. */
    private fun encode(nv21: ByteArray): ByteArray? {
        val square = minOf(previewWidth, previewHeight)
        val left = (previewWidth - square) / 2
        val top = (previewHeight - square) / 2

        val cropped = ByteArrayOutputStream(square * square / 4)
        val yuv = YuvImage(nv21, ImageFormat.NV21, previewWidth, previewHeight, null)
        if (!yuv.compressToJpeg(Rect(left, top, left + square, top + square), jpegQuality, cropped)) {
            Log.w(TAG, "YuvImage.compressToJpeg failed")
            return null
        }

        // Never upscale: if the sensor's square is smaller than the API's
        // recommended 768, send it as-is rather than inventing pixels.
        val target = minOf(Protocol.VIDEO_EDGE, square)
        if (target == square) return cropped.toByteArray()

        val bytes = cropped.toByteArray()
        val decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ?: return bytes
        return try {
            val scaled = Bitmap.createScaledBitmap(decoded, target, target, true)
            val out = ByteArrayOutputStream(target * target / 4)
            scaled.compress(Bitmap.CompressFormat.JPEG, jpegQuality, out)
            if (scaled !== decoded) scaled.recycle()
            out.toByteArray()
        } finally {
            decoded.recycle()
        }
    }

    companion object {
        private const val TAG = "EchoCamera"

        /** Sample every Nth luma byte for the shutter check. */
        private const val LUMA_SAMPLE_STRIDE = 37

        /** Mean luma below this, with no highlights, reads as a closed shutter. */
        private const val NEAR_BLACK_MEAN = 12.0
        private const val NEAR_BLACK_PEAK = 40

        /** Do not spam the model with shutter notices once per frame. */
        private const val SHUTTER_REPORT_INTERVAL_MS = 15_000L
    }
}
