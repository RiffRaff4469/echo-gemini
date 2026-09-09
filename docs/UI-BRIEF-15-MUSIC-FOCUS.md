# UI-BRIEF-15 — Music page (owner round-1: supersedes the "music-focus takeover")

Repo: echo-gemini. Read `docs/HANDOFF.md`, `docs/SPOTIFY.md`,
`docs/UI-BRIEF-14-LAYOUT-ENGINE.md` and **`docs/HOME-MODEL-OWNER-SPEC.md`**
(the owner home model that governs this) first.

**Status (2026-09-08 round-1):** the original brief proposed a full-screen
music *hero that displaces the home* whenever music is active. The owner home
model shelved that: the home is the resting surface and the clock is always
visible during idle, so music lives as a compact rail chip. Round-1 adds a
full music **page** that lives *on top of* the home like the other detail
pages (weather / world clock), with two entry paths:

1. **Chip → page (user-driven).** The rail's now-playing chip is a button.
   Tapping it opens the full music page.
2. **Chat-end landing (server-driven).** When a conversation ends while the
   display's speaker card is still up, the server's `layout {focus: music}`
   push arrives right after `state {state: idle}`; the WebView opens the
   music page within that beat (3 s window) instead of leaving a plain home.
   Phone-side track starts still keep the chip only — music does not displace
   the resting home on its own.

The page closes back to the home (`page-back`), and is parked like every
other page when a conversation opens.

## The music page

Full-page now-playing surface, same visual language as the chat-hero card:

- album art (server-proxied `art_url`, bounded; placeholder monogram on
  none), title / artist / album, live progress bar + times,
- transport row: prev / toggle / next → the existing v1.5 `media_control`
  messages. Buttons carry `data-ui`, so a press never fires tap-to-talk.
- Progress creeps once per second locally between now_playing pushes and is
  corrected by each push (paused pushes stop the ticker).

## Where it lives

- `EchoTerminal/.../assets/ambient/index.html` — `<section data-page="music">`
  + the np chip is now a `<button data-open="music">`.
- `.../app.js` — `drawMusicPage()`, `absorbIdleNp()`, the
  `kickNpTick()/npTickOnce()` ticker family (owns its own timer so it never
  collides with card countdowns), `openPage('music')` hooks, and the
  chat-end landing in `applyFocus()` (gated by `endOfConvoAt`).
- `.../styles.css` — `.music-page` / `.m-art` / `.m-meta` layout.

## Arbitration (unchanged, server)

`chat` while a session runs > `music` while the speaker card is up > `home`
otherwise (server `_current_focus`, UI-BRIEF-14). A session pauses music
(hold); when it ends and playback resumes the focus flips back to music, so
the landing above triggers exactly when the user asked for music in chat.

## Verification (2026-09-08, browser-level)

- idle push → chip only; `layout {focus: music}` with no conversation does
  NOT open the page (home model preserved);
- conversation + now_playing hero → idle → `layout music` lands the page and
  progress ticks 1 s; transport buttons emit `media:…` and never `tap`;
- chip tap opens the page; paused push stops the ticker; page-back returns
  home; music page fits the 960x480 panel without overflow.

## On-device

"play some lofi" (music starts, chip appears) → say "stop" mid-song → page
lands; tap the chip from the resting home → page opens, pause/skip work;
tap ‹ Home → resting home with the chip. Watch art decode once on the 1 GB
device (the page art is the same bounded server-proxied URL as the card).
