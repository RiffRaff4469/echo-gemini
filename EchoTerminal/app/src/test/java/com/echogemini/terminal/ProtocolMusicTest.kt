package com.echogemini.terminal

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The music half of protocol v1.5, checked against `server/protocol.py`.
 *
 * This file is a hand-mirror of the server's protocol module, so nothing but a
 * test notices when the two drift. The two ways they can drift both cost real
 * debugging: a renamed media action reaches the server as an unknown string and
 * the button silently does nothing, and a changed channel byte sends music into
 * [AudioPlayback], which closes the microphone for as long as the album plays.
 *
 * Robolectric because [Protocol] uses `org.json` and `android.util.Log`, both of
 * which are stubs that throw on a bare JVM.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30])
class ProtocolMusicTest {

    // --- the transport row --------------------------------------------------

    @Test fun everyMediaActionSurvivesTheRoundTrip() {
        // The ambient asset sends the wire string it finds in `data-media`, and
        // AmbientWeb.Bridge parses it back with `from`. A value that does not
        // survive this is a button that does nothing at all.
        for (action in Protocol.MediaAction.values()) {
            assertEquals(action, Protocol.MediaAction.from(action.wire))
        }
    }

    @Test fun theWireNamesAreTheOnesTheServerParses() {
        // server/protocol.py MediaAction. Spelled out rather than derived so a
        // rename on this side fails here instead of at the far end of a socket.
        assertEquals(
            listOf("toggle", "pause", "resume", "next", "previous"),
            Protocol.MediaAction.values().map { it.wire }
        )
    }

    @Test fun anUnknownActionIsRejectedRatherThanForwarded() {
        // The bridge must not become a way to put an arbitrary string on the
        // wire: a pushed card's script that reached it could otherwise send
        // anything the server is willing to parse.
        assertNull(Protocol.MediaAction.from("shuffle"))
        assertNull(Protocol.MediaAction.from(""))
        assertNull(Protocol.MediaAction.from(null))
        assertNull(Protocol.MediaAction.from("PAUSE"))
    }

    @Test fun mediaControlCarriesTheActionInAV1Envelope() {
        val body = JSONObject(Protocol.mediaControl(Protocol.MediaAction.NEXT))
        assertEquals(Protocol.VERSION, body.getInt("v"))
        assertEquals("media_control", body.getString("t"))
        assertEquals("next", body.getString("action"))
    }

    @Test fun mediaControlIsDecodableByOurOwnDecoder() {
        // The envelope this end writes and the envelope it reads are the same
        // shape, so the decoder is a usable stand-in for the server's parser.
        val decoded = Protocol.decode(Protocol.mediaControl(Protocol.MediaAction.TOGGLE))
        assertEquals("media_control", decoded?.type)
        assertEquals("toggle", decoded?.str("action"))
    }

    // --- the music channel --------------------------------------------------

    @Test fun musicHasItsOwnChannelDistinctFromSpeech() {
        // The whole reason MusicPlayback exists. Sharing AUDIO_DOWN would hold
        // the mic shut while music played and let barge-in flush the album.
        assertEquals(0x04.toByte(), Protocol.Channel.AUDIO_MUSIC)
        assertNotEquals(Protocol.Channel.AUDIO_DOWN, Protocol.Channel.AUDIO_MUSIC)
    }

    @Test fun aMusicFrameDecodesToItsChannelAndPayload() {
        val pcm = ByteArray(480) { (it % 251).toByte() }
        val frame = Protocol.decodeFrame(
            Protocol.encodeFrame(Protocol.Channel.AUDIO_MUSIC, 7, pcm)
        )
        assertEquals(Protocol.Channel.AUDIO_MUSIC, frame?.channel)
        assertEquals(7, frame?.seq)
        assertArrayEquals(pcm, frame?.payload)
    }

    @Test fun anEmptyMusicFrameIsHeaderOnlyNotNull() {
        // librespot's converter returns b"" for a chunk too short to produce an
        // output sample. The server drops those, but a zero-length payload must
        // decode rather than read as a corrupt frame if one ever arrives.
        val frame = Protocol.decodeFrame(
            Protocol.encodeFrame(Protocol.Channel.AUDIO_MUSIC, 0, ByteArray(0))
        )
        assertEquals(Protocol.Channel.AUDIO_MUSIC, frame?.channel)
        assertEquals(0, frame?.payload?.size)
    }

    // --- the card -----------------------------------------------------------

    @Test fun theNowPlayingCardTypeIsRecognised() {
        assertEquals(Protocol.DisplayType.NOW_PLAYING, Protocol.DisplayType.from("now_playing"))
    }

    @Test fun aNowPlayingPushWithNoDurationStaysUntilReplaced() {
        // SpotifyController pushes duration=0: the card is replaced by the next
        // track or cleared when playback stops, never by a timer. A non-zero
        // durationMs here would hide the card mid-song.
        val msg = Protocol.decode(
            JSONObject()
                .put("v", Protocol.VERSION)
                .put("t", Protocol.Type.DISPLAY)
                .put("type", "now_playing")
                .put("payload", JSONObject().put("title", "Blue Monday").put("is_playing", true))
                .put("duration", 0.0)
                .put("priority", 0)
                .toString()
        )
        val cmd = Protocol.DisplayCommand.from(msg!!)
        assertEquals(Protocol.DisplayType.NOW_PLAYING, cmd?.type)
        assertEquals(0L, cmd?.durationMs)
        assertEquals("Blue Monday", cmd?.payload?.getString("title"))
    }

    @Test fun helloAdvertisesTheMusicMinorVersion() {
        // The minor version is how the server knows this build can render
        // AUDIO_MUSIC at all; shipping the music code without bumping it is a
        // silent mismatch that only shows up as frames the device logs and drops.
        val body = JSONObject(
            Protocol.hello("echo-show-5", "0.1.0", JSONObject().put("music", true))
        )
        assertEquals(5, body.getInt("protocol_minor"))
        assertEquals(true, body.getJSONObject("capabilities").getBoolean("music"))
    }
}
