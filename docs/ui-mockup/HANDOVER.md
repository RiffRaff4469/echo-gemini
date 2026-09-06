# Echo Show 5 (Gen 1) — Custom UI Concept: Design Handover

**Artifact:** `echo_show_ui_mockup.html` (single-file HTML/CSS/JS, no build step, no external dependencies except two Google Fonts)
**Target hardware:** Amazon Echo Show 5, 1st gen — 5.5" screen, 960×480 resolution, 2:1 aspect ratio, no capacitive multi-touch gestures beyond basic tap/swipe
**Status:** Interactive concept mockup, not yet wired to real data or a real launcher

This document exists so another AI (or a human) can understand *why* things were built the way they were, not just *what* was built, and can pick up the work — whether that means refining the visuals, porting it to a real display pipeline (Flutter/Kivy/web-kiosk/custom launcher), or wiring it to live data.

---

## 1. Brief, as understood

The person wanted an ambient "ready to glance at from across the room" display for a rooted/custom-launcher Echo Show 5, with:

1. A big, legible clock, date, and weather — readable at distance.
2. A status dot indicating whether the device's backend/server connection is alive — **color only, no text**, since a glance shouldn't require reading a label.
3. A glow "ring" around the screen edge that lights up while the assistant is listening/speaking, evoking the Alexa light ring but framing the whole display instead of a physical ring on the base.
4. Multiple swipeable "pages" beyond the clock: sports scores, weather detail, and open-ended "other cool UI" — which became flights overhead, breaking news, stocks, and a world clock.
5. Several clock face styles, explicitly modeled on iPhone StandBy mode (Digital / Analog / Bold-color / Photo-style treatments).
6. A world-clock map like Apple's, with a day/night terminator line and dots for saved cities.

Each of these arrived as an iterative round; the file was rewritten in place each time rather than layered as patches, to keep the CSS/JS internally consistent.

---

## 2. Design system (tokens)

Defined as CSS custom properties at the top of the file — change these once, everything downstream follows:

| Token | Value | Role |
|---|---|---|
| `--void` | `#05070a` | Base screen background (true near-black, since OLED-style ambient displays should stay dark) |
| `--bezel` | `#111418` | Physical device bezel color |
| `--fabric` | `#1c1f24` | Fabric-wrapped base of the device (textured, see §3) |
| `--ink` / `--ink-dim` / `--ink-faint` | `#f2f4f7` @ 100%/55%/32% | Text hierarchy via opacity, not separate greys — keeps everything color-harmonic |
| `--blue` | `#4f8cff` | "System" accent: listening ring, connection-good, world clock dots. Deliberately not Alexa's exact cyan, to feel distinct while staying in the same family |
| `--amber` | `#ffb454` | Weather/sun accent, retro clock face glow |
| `--green` / `--red` | `#35d399` / `#ff5c5c` | Connection dot states, stock up/down, live-game indicators |
| `--purple` | `#b06bff` | Used only in the Bold clock face gradient |

**Type:** `Space Grotesk` for all numerals (clock, scores, prices) — a geometric face with a slightly technical character that stays legible at very large sizes. `Inter` for body/labels. `JetBrains Mono` only for the Retro clock face, to sell the "nixie tube" read.

**Why not a templated look:** avoided the generic "cream background + serif" and "SaaS card" defaults (see the `frontend-design` skill's calibration list) — dark ambient-display convention was chosen because it's what the actual product category (bedside/kitchen smart displays) uses, and because a 5.5" panel viewed at a distance needs high contrast rather than delicate color washes.

---

## 3. The device chrome

The mockup doesn't just render a screenshot — it renders a *device*:

- Outer `.device` div uses a CSS gradient background split ~74/26 to fake the seam between the plastic bezel and the fabric-wrapped base speaker, matching the real hardware's proportions.
- A single dark dot (`.cam`) top-center represents the camera, since Gen 1 has a visible front camera in the bezel.
- `.fabric-tex` is a repeating diagonal gradient at a tight interval to fake woven fabric texture without an image asset.
- `.screen-wrap` is the actual 960×480 canvas — everything UI-relevant lives inside this, at the device's real aspect ratio, so proportions in the mockup match the physical product.
- The pagination dots live **in the fabric base**, not on the screen — this was a deliberate choice so the screen's usable area stays 100% content, the way a real ambient display would want it, and paging feels like a hardware affordance rather than a screen element competing for space.

---

## 4. Interaction model

All page/state switching is done via CSS class toggles driven by small vanilla-JS event listeners — no framework, so it will paste directly into any WebView-based launcher.

- **Pages** are `.screen[data-page="n"]` siblings, only one has `.visible` at a time. `goTo(i)` toggles both the screen and the matching bottom pagination dot. Both the bottom dots and any external button with `data-goto="n"` call the same function — this means adding a hardware swipe gesture later just needs one call to `goTo()`.
- **Listening ring** (`#ring`) is an absolutely-positioned overlay with `inset: 0` and an inset box-shadow; toggling `.active` starts a `pulse` keyframe animation. This was kept as a pure CSS animation (not JS-driven opacity loops) so it stays performant on modest hardware.
- **Connection dot**: originally had a text label ("Connected"/"Offline") next to it; per feedback this was **removed entirely** — the dot's fill color and glow are the only signal. All dots share the class `.conn-dot` so one toggle updates every page's dot at once (they don't reset when you change pages).
- **Clock faces**: four `.face` divs share one `.clock-stage` container; only one has `.active`. A tiny pill-button row (`.face-switch`) swaps them. All four faces are updated from a single `tick()` function every second, so switching faces never shows stale time.

---

## 5. Page-by-page rationale

### Home
Clock face + date dominate the upper two-thirds; a thin status row up top (dot + device name); a footer row with a compact weather chip and a hint nudging the user to swipe. The face-switcher lives here rather than in a settings menu, because on a glanceable ambient display, style should be a tap away, not buried.

### Clock faces (iPhone StandBy–inspired)
- **Digital** — tabular-nums, `Space Grotesk`, the safest "read across the room" default.
- **Analog** — real SVG clock, hands computed and rotated every second via `setHand()`, which converts hours/minutes/seconds into rotation angles from a shared 65,65 center. Chosen because StandBy's analog face is one of its most-used looks and it's a nice contrast to an all-digital display.
- **Bold** — oversized gradient-filled numerals (`background-clip: text`), directly mirroring StandBy's saturated full-bleed color style.
- **Retro** — monospace, amber glow, live seconds — a "nixie tube alarm clock" read, included as the stylistic opposite of Bold, so the four faces span a real range rather than four minor variations on one idea.

### Weather
Large current temp, sun icon, hi/lo/wind, and a 6-hour strip with per-hour condition dots (color-coded from amber-sun to blue-grey-cloud) — deliberately denser than Home's weather chip, since this page's whole job is weather detail.

### Scores
Three score-card rows (NFL, college football, NBA) demonstrating both **live** (red pulsing "LIVE" + game clock) and **final** states, colored team badges standing in for real logos. Built generic enough that swapping in a real sports API only means changing the data going into these rows, not the markup structure.

### Flights overhead
The one genuinely novel widget: a small **radar view** (concentric range rings + a center "you are here" dot) with rotated triangular plane markers showing heading, paired with a callsign/route/altitude/speed list underneath. This is the kind of thing a Flightradar24-style API could feed directly — position → radar angle/distance, heading → marker rotation.

### News
One large "BREAKING" headline up top (for whatever's most urgent), with a continuously scrolling marquee ticker of secondary headlines underneath, built as a duplicated/looping CSS `translateX` animation — a pattern that reads instantly as "news ticker" without needing a real feed wired in yet.

### Stocks
Symbol, name, a small static SVG sparkline (green if trending up, red if down), price, and percent change — modeled loosely on Apple's Stocks widget, kept information-dense since price data benefits from precision over big type.

### World Clock (most recent addition)
- A stylized dark equirectangular **world map** — continents are hand-drawn simplified blob paths (not a real GeoJSON import), since at 960×480 on a 5.5" screen, coastline fidelity doesn't matter but visual clarity does.
- **Terminator line**: computed for real using actual solar geometry — solar declination from day-of-year (`23.44° · sin(2π/365 · (284 + dayOfYear))`), subsolar longitude from current UTC time, then for each longitude sample, `tan(lat) = -cos(H) / tan(dec)` gives the boundary latitude. This redraws correctly for the real current date and time, not a static illustration.
- **Night shading**: a soft radial gradient centered on the antisolar point (the exact opposite of the sun's position), stretched into an ellipse — a deliberate simplification instead of computing a pixel-accurate day/night polygon, since the visual read (a soft dark region sweeping across the map, like Apple's own World Clock) matters more than per-pixel accuracy here.
- **City dots**: lat/lon converted to screen `x%/y%` via simple equirectangular projection (`x=(lon+180)/360`, `y=(90-lat)/180`), currently defaulted to Syracuse, Singapore, Madrid, and Rome as example saved locations. Below the map, a card per city shows live local time (via `Intl.DateTimeFormat` with each city's IANA timezone) and a Today/Tomorrow/Yesterday badge when the date rolls over relative to local time.

---

## 6. Known simplifications / honest limitations

- Continents are stylized blobs, not real coastline data — swap in a proper low-poly GeoJSON→SVG path if geographic accuracy becomes important.
- The night-shadow ellipse is a visual approximation; the terminator *line* itself is accurately computed, but the two aren't perfectly co-registered at the edges. Good enough for an ambient glance, not for a navigation product.
- All data (weather, scores, flights, headlines, stock prices) is hardcoded — nothing is wired to a live API yet. Every page was structured so that swapping static markup for templated/data-bound rows should be straightforward (each row is a small, repeated, self-contained block).
- No swipe/gesture handling is implemented — page changes are click/tap on dots or buttons only. A real build should add touch-swipe (or hardware volume-button paging, if that's viable on this device) bound to the same `goTo()` function.
- Never tested on real Echo Show 5 hardware/WebView — only in a standard desktop browser. Font rendering, animation performance, and touch target sizes should be re-validated on-device before shipping.

---

## 7. Suggested next steps for whoever picks this up

1. Wire real data sources in: weather API, a sports scores API, a flight-tracking API (ADS-B feed or FlightAware/Flightradar24), an RSS/news API, and a stocks API. The markup per page was intentionally kept as small repeated blocks to make this a template-substitution job rather than a rebuild.
2. Add touch-swipe gesture handling between pages (currently dot/button-tap only).
3. Decide the real trigger for the listening ring and connection dot — presumably a WebSocket or polling connection to whatever "server" this device reports its status to (see the person's other in-progress projects: Hermes agent framework, Orbit, etc. — likely the natural backend here).
4. If porting off a plain WebView, note this is zero-dependency HTML/CSS/JS (only two Google Fonts loaded via `<link>`) — should port cleanly into any Chromium-based kiosk launcher without a build step.
5. Consider whether the four clock faces should be user-configurable persistently (localStorage is unavailable in the Claude artifact sandbox this was prototyped in, but is fine on real device hardware) versus reset-on-boot.

---

## 8. File index

- `echo_show_ui_mockup.html` — the full interactive mockup described above.
- This document — design rationale and handover notes.
