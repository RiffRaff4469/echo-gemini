/*
 * The ambient UI's only script. Everything the panel shows lives here.
 *
 * UI-BRIEF-14 (layout engine + owner home model, 2026-09-08): one
 * focus-driven layout. `layout {focus: home|music|chat}` messages from the
 * server pick the template; the home engine (clock hero 65% + widget rail
 * 35%) is the resting surface and the clock NEVER rotates away. Detail pages
 * (weather, world clock, ...) open over the home from rail tiles and close
 * back to it. A conversation uses the chat template (overlay + compact
 * corner clock + pill/ring + answer cards). Music does NOT displace the
 * home: now playing renders as a rail chip while idle, and as the hero card
 * only inside a conversation.
 *
 * Two directions across the bridge, deliberately narrow:
 *
 *   native -> JS   window.Echo.{linkState,uiState,weather,quiet,schedule,
 *                               stopwatch,layout,mute,display,displayClear,
 *                               pageData}
 *   JS -> native   window.EchoNative.{tap,openAlarm,openTimer,openStopwatch,
 *                                     media,log}
 *
 * The JS side owns no state the app cannot rebuild: if the WebView is torn
 * down and reloaded, MainActivity replays the last link state, ui state,
 * weather reading, schedule counts, mute state and layout focus. Nothing
 * here talks to the network -- the device has none.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- bridge
  // Stubbed when the page is opened in a desktop browser for UI iteration, so
  // the asset can be edited and previewed without an APK.
  var NATIVE = window.EchoNative || {
    tap: function () { console.log('tap'); },
    openAlarm: function () { console.log('openAlarm'); },
    openTimer: function () { console.log('openTimer'); },
    openStopwatch: function () { console.log('openStopwatch'); },
    media: function (a) { console.log('media ' + a); },
    select: function (i) { console.log('select ' + i); },
    log: function (m) { console.log(m); }
  };

  var $ = function (id) { return document.getElementById(id); };

  function escapeHtml(raw) {
    return String(raw)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ------------------------------------------------------- layout engine
  // The server owns the focus (protocol v1.6 layout message): home is the
  // resting surface, chat while a session runs, music while the display's
  // speaker card is up. Music renders as the now-playing chip on the home;
  // tapping the chip (or a chat ending while music is up, focus = music)
  // opens the full music page -- a normal detail page that closes back to
  // the home (owner round-1).
  // Detail pages (weather/world/music/...) are pure local navigation on top.
  var PAGE_LABELS = {
    home: 'Home', weather: 'Weather', world: 'World Clock', music: 'Now Playing',
    scores: 'Scores', flights: 'Overhead', news: 'News', stocks: 'Markets'
  };
  var FULL_PAGES = ['weather', 'world', 'scores', 'flights', 'news', 'stocks'];

  var uiState = 'idle';
  var focus = 'home';          // last server layout focus (sanitized)
  var current = 'home';        // the .page currently shown underneath overlays

  function openPage(id) {
    if (!PAGE_LABELS[id]) id = 'home';
    var enteringMusic = id === 'music';
    var leavingMusic = current === 'music' && !enteringMusic;
    current = id;
    var pages = document.querySelectorAll('.page');
    for (var k = 0; k < pages.length; k++) {
      pages[k].classList.toggle('visible', pages[k].dataset.page === id);
    }
    $('page-label').textContent = PAGE_LABELS[id];
    $('page-back').classList.toggle('hidden', id === 'home');
    if (id === 'world') drawWorld();
    if (enteringMusic) {
      musicOpen = true;
      drawMusicPage(true);
      kickNpTick();
    } else if (leavingMusic) {
      musicOpen = false;
      kickNpTick();   // no visible progress surface: the ticker stops itself
    }
  }

  // ---------------------------------------------------------------- focus
  var FOCUSES = { home: 1, music: 1, chat: 1 };

  /** Apply a server layout focus. Unknown values fall back to home. */
  function applyFocus(raw) {
    var next = FOCUSES[raw] ? raw : 'home';
    // Guard: never take the home surface away unless a conversation is
    // actually on screen -- the chat template is the overlay + card chrome,
    // which only exists while uiState is non-idle.
    if (next === 'chat' && uiState === 'idle') return;
    if (next === focus) return;
    focus = next;
    var cls = document.body.classList;
    cls.remove('fx-home', 'fx-music', 'fx-chat');
    cls.add('fx-' + focus);
    // Music is the landing surface when a conversation ends while the
    // speaker card is still up (owner round-1): arrive on the full music
    // page. Plain phone-side starts still keep the chip on the home -- only
    // a focus that follows the end of a conversation within a beat lands
    // here, and only if the user has not navigated somewhere meanwhile.
    if (next === 'music') {
      if (uiState === 'idle' && current === 'home' &&
          Date.now() - endOfConvoAt < 3000 &&
          !npChipEl.classList.contains('hidden')) {
        openPage('music');
      }
    } else if (next === 'home' && current === 'music') {
      // The speaker card cleared (music stopped) while the page was up.
      openPage('home');
    }
    if (next !== 'chat') touchIdle();
  }

  // ---------------------------------------------------------------- clock
  var MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];
  var DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  var DAYS_S = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var MONTHS_S = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function drawClock(now) {
    var h24 = now.getHours();
    var h12 = h24 % 12; if (h12 === 0) h12 = 12;
    var ap = h24 >= 12 ? 'PM' : 'AM';
    // Big hero clock (home template).
    $('time-digital').innerHTML =
      h12 + '<span class="colon">:</span>' + pad(now.getMinutes()) +
      '<span class="ampm">' + ap + '</span>';
    $('date-digital').textContent =
      DAYS[now.getDay()] + ', ' + MONTHS[now.getMonth()] + ' ' + now.getDate();
    // Compact clock (chat template corner block).
    $('time-mini').textContent = h12 + ':' + pad(now.getMinutes()) + ' ' + ap;
    $('date-mini').textContent =
      DAYS_S[now.getDay()] + ' &middot; ' + MONTHS_S[now.getMonth()] + ' ' + now.getDate();
  }

  // --------------------------------------------------------- world clock
  // Editable without an APK rebuild: this file is an asset, and a later brief
  // can replace the list from a config push without touching Kotlin.
  var CITIES = [
    { name: 'Syracuse',  tz: 'America/New_York', lat: 43.05, lon: -76.15, hero: true },
    { name: 'Singapore', tz: 'Asia/Singapore',   lat: 1.35,  lon: 103.82 },
    { name: 'Geneva',    tz: 'Europe/Zurich',    lat: 46.20, lon: 6.14 }
  ];
  var HERO = CITIES.filter(function (c) { return c.hero; })[0] || CITIES[0];

  function cityCode(c) { return c.name.slice(0, 3).toUpperCase(); }

  function cityTime(city, now) {
    try {
      return new Intl.DateTimeFormat('en-US', {
        hour: 'numeric', minute: '2-digit', timeZone: city.tz
      }).format(now);
    } catch (e) {
      return '--:--';
    }
  }

  function cityDayBadge(city, now) {
    try {
      var here = now.toLocaleDateString('en-US');
      var there = now.toLocaleDateString('en-US', { timeZone: city.tz });
      if (there === here) return 'Today';
      return new Date(there) > new Date(here) ? 'Tomorrow' : 'Yesterday';
    } catch (e) {
      return '';
    }
  }

  var cityListEl = $('city-list');
  var railCitiesEl = $('rail-cities');
  CITIES.forEach(function (c) {
    var card = document.createElement('div');
    card.className = 'city-card';
    card.innerHTML = '<div class="cname">' + escapeHtml(c.name) + '</div>' +
                     '<div class="ctime">--:--</div><div class="cbadge"></div>';
    cityListEl.appendChild(card);
    // Compact one-liner for the home rail: "SYR   9:41 PM".
    var row = document.createElement('span');
    row.className = 'wc-row';
    row.innerHTML = '<span class="wc-code">' + cityCode(c) + '</span>' +
                    '<span class="wc-time">--:--</span>';
    railCitiesEl.appendChild(row);
  });

  function drawCities(now) {
    for (var i = 0; i < CITIES.length; i++) {
      var card = cityListEl.children[i];
      card.children[1].textContent = cityTime(CITIES[i], now);
      card.children[2].textContent = cityDayBadge(CITIES[i], now);
    }
    $('hero-name').textContent = HERO.name.toUpperCase();
    $('hero-time').textContent = cityTime(HERO, now);
    $('hero-date').textContent = DAYS[now.getDay()] + ', ' + MONTHS[now.getMonth()] + ' ' + now.getDate();
  }

  function drawRailCities(now) {
    for (var i = 0; i < CITIES.length; i++) {
      var row = railCitiesEl.children[i];
      row.children[1].textContent = cityTime(CITIES[i], now);
    }
  }

  var worldDrawnAt = 0;
  function drawWorld() {
    var now = new Date();
    drawCities(now);
    // The terminator moves a quarter of a degree a minute: redrawing the dot
    // field more often than that is spending CPU on nothing visible.
    if (Date.now() - worldDrawnAt < 55000) return;
    try {
      // Only counts as drawn if it drew: the canvas has no size until the
      // page is first shown, and a failed measurement must retry, not sleep.
      if (window.World.render(now, CITIES, HERO.name)) worldDrawnAt = Date.now();
    } catch (e) {
      NATIVE.log('world map render failed: ' + e);
    }
  }

  // --------------------------------------------------------------- weather
  // Mirrors Protocol.Weather in the client and server/weather.py: WMO 4677
  // codes collapse to seven drawable shapes, and the wording carries detail.
  function condition(code) {
    if (code === 0) return 'clear';
    if (code === 1 || code === 2) return 'partly';
    if (code === 3) return 'cloud';
    if (code === 45 || code === 48) return 'fog';
    if ([51,53,55,56,57,61,63,65,66,67,80,81,82].indexOf(code) >= 0) return 'rain';
    if ([71,73,75,77,85,86].indexOf(code) >= 0) return 'snow';
    if ([95,96,99].indexOf(code) >= 0) return 'storm';
    return 'cloud';  // an unknown code is likelier cloud than sunshine
  }

  var LABELS = {
    0: 'Clear', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Fog', 51: 'Drizzle', 53: 'Drizzle', 55: 'Drizzle',
    56: 'Freezing drizzle', 57: 'Freezing drizzle', 61: 'Rain', 63: 'Rain',
    65: 'Rain', 66: 'Freezing rain', 67: 'Freezing rain', 71: 'Snow',
    73: 'Snow', 75: 'Snow', 77: 'Snow grains', 80: 'Showers', 81: 'Showers',
    82: 'Showers', 85: 'Snow showers', 86: 'Snow showers',
    95: 'Thunderstorm', 96: 'Storm with hail', 99: 'Storm with hail'
  };

  var A = '#ffb454', G = '#c8d2e0';

  function glyph(cond, isDay) {
    var body = '';
    var sunOrMoon = isDay
      ? '<circle cx="12" cy="12" r="5" fill="' + A + '"/>' +
        '<g stroke="' + A + '" stroke-width="1.6" stroke-linecap="round">' +
        '<line x1="12" y1="1" x2="12" y2="3.4"/><line x1="12" y1="20.6" x2="12" y2="23"/>' +
        '<line x1="1" y1="12" x2="3.4" y2="12"/><line x1="20.6" y1="12" x2="23" y2="12"/>' +
        '<line x1="4.2" y1="4.2" x2="5.9" y2="5.9"/><line x1="18.1" y1="18.1" x2="19.8" y2="19.8"/>' +
        '<line x1="4.2" y1="19.8" x2="5.9" y2="18.1"/><line x1="18.1" y1="5.9" x2="19.8" y2="4.2"/></g>'
      : '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a7.5 7.5 0 1 0 10.5 10.5z" fill="' + A + '"/>';
    var cloud = '<path d="M7.5 19h9a4 4 0 0 0 .4-8 5.5 5.5 0 0 0-10.6 1.3A3.4 3.4 0 0 0 7.5 19z" fill="' + G + '"/>';
    if (cond === 'clear') body = sunOrMoon;
    else if (cond === 'partly') body = '<g transform="translate(-2,-3) scale(0.8)">' + sunOrMoon + '</g>' + cloud;
    else if (cond === 'cloud') body = cloud;
    else if (cond === 'fog') body = cloud +
      '<g stroke="' + G + '" stroke-width="1.5" stroke-linecap="round" opacity="0.75">' +
      '<line x1="5" y1="21.5" x2="14" y2="21.5"/><line x1="16.5" y1="21.5" x2="19" y2="21.5"/></g>';
    else if (cond === 'rain') body = cloud +
      '<g stroke="#7fb3ff" stroke-width="1.7" stroke-linecap="round">' +
      '<line x1="9" y1="20.5" x2="8" y2="23"/><line x1="13" y1="20.5" x2="12" y2="23"/>' +
      '<line x1="17" y1="20.5" x2="16" y2="23"/></g>';
    else if (cond === 'snow') body = cloud +
      '<g fill="#dbe6f5"><circle cx="9" cy="21.6" r="1.1"/><circle cx="13" cy="21.6" r="1.1"/>' +
      '<circle cx="17" cy="21.6" r="1.1"/></g>';
    else body = cloud + '<path d="M13 19.5l-3.4 0 3.2 4.5-0.6-3 3.3 0-3.4-4.6z" fill="' + A + '"/>';

    return '<svg viewBox="0 0 24 26" xmlns="http://www.w3.org/2000/svg">' + body + '</svg>';
  }

  function fahrenheit(c) { return Math.round(c * 9 / 5 + 32) + '°'; }

  function applyWeather(reading) {
    var cond = condition(reading.code);
    var label = LABELS[reading.code] || 'Cloudy';
    var temp = fahrenheit(reading.temp_c);
    var svg = glyph(cond, reading.is_day !== false);

    $('home-wtemp').textContent = temp;
    $('home-wcond').textContent = label;
    $('home-wico').innerHTML = svg;
    $('wx-temp').textContent = temp;
    $('wx-sub').textContent = label;
    $('wx-glyph').innerHTML = svg;
  }

  // ------------------------------------------------------- pushed cards
  // The same display types PushSurface renders natively, with the same
  // priority and duration rules, so the server sees no behaviour change.
  var cardEl = $('card');
  var cardBody = $('card-body');
  var cardShowing = false;
  var cardPriority = -1e9;
  var cardExpiry = null;
  var cardTimer = null;
  var cardType = null;
  var cardKey = null;

  function cardPage(inner) { return '<div class="card-wrap">' + inner + '</div>'; }

  function renderCard(type, p) {
    if (type === 'text') {
      return cardPage('<div class="card-main">' + escapeHtml(p.text) + '</div>' +
        (p.subtitle ? '<div class="card-sub">' + escapeHtml(p.subtitle) + '</div>' : ''));
    }
    if (type === 'image') {
      return cardPage('<img class="card-img" src="' + escapeHtml(p.url) + '" alt="">');
    }
    if (type === 'now_playing') return nowPlayingCard(p);
    if (type === 'options') return optionsCard(p);
    if (type === 'list') return listCard(p);
    if (type === 'timer') {
      return cardPage('<div class="card-sub">' + escapeHtml(p.label || '') + '</div>' +
        '<div class="card-main card-count" id="card-count">--:--</div>');
    }
    return null;   // html is handled separately: it goes in a sandbox
  }

  // ------------------------------------------- interactive panels (v1.6)
  // UI-BRIEF-16: OPTIONS renders big tappable choice rows; LIST renders a
  // read-only enumeration. Both are TRUSTED DOM rendered by this page from a
  // typed payload -- never server HTML, same trust class as text/timer cards.
  // Rows are >=54 px (well over the 48 px floor), mark themselves data-ui so
  // a tap is NOT tap-to-talk, and send NATIVE.select(index) -- deliberately a
  // separate message from tap/button: since v1.6 talk belongs to the mic
  // button and the wake word. The server maps the index back to the label.

  function optionsCard(p) {
    var head = p.question || p.title || 'Choose one';
    var rows = (p.options || []).map(function (o, i) {
      var sub = o.sub
        ? '<span class="opt-sub">' + escapeHtml(String(o.sub)) + '</span>' : '';
      return '<button class="opt-row" type="button" data-ui data-select="' + i + '">' +
        '<span class="opt-idx">' + (i + 1) + '</span>' +
        '<span class="opt-main"><span class="opt-label">' +
        escapeHtml(String(o.label)) + '</span>' + sub + '</span>' +
        '</button>';
    }).join('');
    return cardPage('<div class="panel opt-panel">' +
      '<div class="panel-head">' + escapeHtml(String(head)) + '</div>' +
      '<div class="panel-rows">' + rows + '</div></div>');
  }

  function listCard(p) {
    var title = p.title;
    var rows = (p.items || []).map(function (it, i) {
      return '<div class="list-row"><span class="list-idx">' + (i + 1) + '</span>' +
        '<span class="list-item">' + escapeHtml(String(it)) + '</span></div>';
    }).join('');
    return cardPage('<div class="panel list-panel">' +
      (title ? '<div class="panel-head">' + escapeHtml(String(title)) + '</div>' : '') +
      '<div class="panel-rows">' + rows + '</div></div>');
  }

  // --------------------------------------------------- now playing (v1.5)
  // The full card is the CHAT-hero form of now playing: it renders only while
  // a conversation is on screen (and during idle it is instead a compact
  // rail chip -- music never displaces the home, owner home model spec).
  // Buttons are >=64 px because they are pressed with a thumb, in the dark,
  // by someone who is not looking closely.
  //
  // Nothing here is optimistic. Pressing pause sends `pause` and changes
  // nothing on screen; the card redraws when the server's next now_playing
  // push says the player actually stopped. The player is on the PC and it is
  // the only thing that knows.
  var ICONS = {
    previous: '<path d="M4.5 5h2.5v14H4.5zM20 5v14L9 12z"/>',
    next: '<path d="M17 5h2.5v14H17zM4 5v14l11-7z"/>',
    play: '<path d="M8 5v14l11-7z"/>',
    pause: '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>'
  };

  function icon(name) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + ICONS[name] + '</svg>';
  }

  function clockText(seconds) {
    var s = Math.max(0, Math.round(seconds || 0));
    return Math.floor(s / 60) + ':' + pad(s % 60);
  }

  /** Identity of the track on screen, so a re-push updates instead of reloading. */
  function nowPlayingKey(p) {
    return (p.title || '') + '\u0000' + (p.artist || '');
  }

  function nowPlayingCard(p) {
    var art = p.art_url
      ? '<img class="np-art" src="' + escapeHtml(p.art_url) + '" alt="">'
      : '<div class="np-art np-noart">' + icon('play') + '</div>';
    return '<div class="np">' + art +
      '<div class="np-meta">' +
      '<div class="np-title">' + escapeHtml(p.title || '') + '</div>' +
      '<div class="np-artist">' + escapeHtml(p.artist || '') + '</div>' +
      '<div class="np-album">' + escapeHtml(p.album || '') + '</div>' +
      '<div class="card-bar"><i id="np-bar"></i></div>' +
      '<div class="np-times"><span id="np-pos">0:00</span>' +
      '<span id="np-dur">' + clockText(p.duration_s) + '</span></div>' +
      '<div class="np-row">' +
      '<button class="np-btn" data-ui data-media="previous">' + icon('previous') + '</button>' +
      '<button class="np-btn np-main" data-ui data-media="toggle" id="np-toggle"></button>' +
      '<button class="np-btn" data-ui data-media="next">' + icon('next') + '</button>' +
      '</div></div></div>';
  }

  // Progress as the page last understood it. The bar is advanced locally
  // between pushes so it creeps rather than jumping every few seconds; the
  // server's next push is always what corrects it.
  var npProgress = 0;
  var npDuration = 0;
  var npPlaying = false;
  var npLast = null;     // the last full now_playing payload, for the music page
  var npTickTimer = null;

  function applyNowPlaying(p) {
    npLast = p;
    npDuration = Math.max(0, p.duration_s || 0);
    npProgress = Math.min(Math.max(0, p.progress_s || 0), npDuration || Infinity);
    npPlaying = p.is_playing !== false;
    var toggle = $('np-toggle');
    if (toggle) toggle.innerHTML = icon(npPlaying ? 'pause' : 'play');
    var duration = $('np-dur');
    if (duration) duration.textContent = clockText(npDuration);
    drawNowPlaying();
    if (musicOpen && current === 'music') drawMusicPage();
    kickNpTick();
  }

  /** Absorb a now_playing push that arrived while the display is idle. */
  function absorbIdleNp(p) {
    npLast = p;
    npDuration = Math.max(0, p.duration_s || 0);
    npProgress = Math.min(Math.max(0, p.progress_s || 0), npDuration || Infinity);
    npPlaying = p.is_playing !== false;
    if (musicOpen && current === 'music') drawMusicPage();
    // Music arriving is engagement, and it must not hide behind an ambient
    // page: drop any rotation and reset the idle clock.
    touchIdle();
    kickNpTick();
  }

  function drawNowPlaying() {
    var bar = $('np-bar');
    var pos = $('np-pos');
    if (bar) {
      bar.style.width =
        (npDuration > 0 ? Math.min(100, npProgress / npDuration * 100) : 0).toFixed(1) + '%';
    }
    if (pos) pos.textContent = clockText(npProgress);
  }

  /**
   * (Re)arm the once-per-second progress ticker, but only while a surface
   * that shows progress (the chat-hero card or the music page) is actually
   * visible. Owns its own timer so it never collides with card countdowns.
   */
  function kickNpTick() {
    if (npTickTimer) { clearTimeout(npTickTimer); npTickTimer = null; }
    if (!npPlaying || npDuration <= 0) return;
    if ((cardShowing && cardType === 'now_playing') ||
        (current === 'music' && musicOpen)) {
      npTickTimer = setTimeout(npTickOnce, 1000);
    }
  }

  function npTickOnce() {
    npTickTimer = null;
    if (!npPlaying || npDuration <= 0) return;
    if (!((cardShowing && cardType === 'now_playing') ||
          (current === 'music' && musicOpen))) return;  // nothing visible: stop
    npProgress = Math.min(npProgress + 1, npDuration);
    drawNowPlaying();
    if (current === 'music') drawMusicPage();
    npTickTimer = setTimeout(npTickOnce, 1000);
  }

  // ------------------------------------------------------- music page
  // The full now-playing surface (owner round-1). It is an ordinary detail
  // page: opened by tapping the rail chip, and auto-opened when layout
  // focus = music lands on an idle home right after a conversation ends.
  // Same payload language as the chat-hero card; the transport row sends
  // the same media_control messages (handled document-wide, below).
  var musicOpen = false;
  var mTitle = $('m-title'), mArtist = $('m-artist'), mAlbum = $('m-album'),
      mArt = $('m-art'), mBar = $('m-bar'), mPos = $('m-pos'),
      mDur = $('m-dur'), mToggle = $('m-toggle');
  var npPageKey = '';    // the track identity the page last fully rendered

  function musicKey(p) {
    if (!p) return '';
    return ((p.title || '') + '\u0000' + (p.artist || '') + '\u0000' +
            (p.art_url || ''));
  }

  function musicProgressPct() {
    return (npDuration > 0 ? Math.min(100, npProgress / npDuration * 100) : 0).toFixed(1) + '%';
  }

  function drawMusicPage(force) {
    if (npLast && musicKey(npLast) !== npPageKey) force = true;
    if (force) {
      npPageKey = musicKey(npLast);
      mTitle.textContent = (npLast && npLast.title) ? npLast.title : 'Nothing playing';
      mArtist.textContent = (npLast && npLast.artist) ? npLast.artist : '';
      mAlbum.textContent = (npLast && npLast.album) ? npLast.album : '';
      mArt.innerHTML = (npLast && npLast.art_url)
        ? '<img src="' + escapeHtml(npLast.art_url) + '" alt="">'
        : '<div class="np-noart">' + icon('play') + '</div>';
      mDur.textContent = clockText(npDuration);
    } else {
      mDur.textContent = clockText(npDuration);
    }
    mPos.textContent = clockText(npProgress);
    mBar.style.width = musicProgressPct();
    mToggle.innerHTML = icon(npPlaying ? 'pause' : 'play');
  }

  // ------------------------------------------------------ rail np chip
  // The home-model form of now playing: one clipped "title — artist" line
  // with an equaliser dot. Kept in sync by every now_playing push, shown
  // only while the speaker card is up, hidden by display_clear. A button:
  // tapping it opens the full music page.
  var npChipEl = $('np-chip');
  var npChipText = $('np-chip-text');

  function applyRailNp(p) {
    npChipText.textContent =
      '\u266A ' + [p.title, p.artist].filter(Boolean).join(' \u2014 ') || '\u266A';
    npChipEl.classList.toggle('paused', p.is_playing === false);
    npChipEl.classList.remove('hidden');
  }

  function hideRailNp() {
    npChipEl.classList.add('hidden');
    npChipText.textContent = '\u266A';
    npLast = null;
    npPageKey = '';
  }

  /** Drop a now-playing overlay card (chat hero) back to its rail chip. */
  function collapseNpOverlay() {
    if (!cardShowing) return;
    clearCardTimers();
    cardShowing = false;
    cardPriority = -1e9;
    cardType = null;
    cardKey = null;
    cardEl.classList.remove('shown');
    setTimeout(function () {
      if (cardShowing) return;
      cardEl.classList.add('hidden');
      cardBody.innerHTML = '';   // let the pushed page go, RAM is not free
    }, 340);
  }

  function startCountdown(seconds) {
    var left = Math.max(0, Math.round(seconds || 0));
    var el = $('card-count');
    if (!el) return;
    var draw = function () {
      var text = Math.floor(left / 60) + ':' + pad(left % 60);
      if (left >= 3600) {
        text = Math.floor(left / 3600) + ':' + pad(Math.floor(left / 60) % 60) + ':' + pad(left % 60);
      }
      el.textContent = text;
      if (left <= 0) { el.style.color = '#ff5c5c'; return; }
      left -= 1;
      cardTimer = setTimeout(draw, 1000);
    };
    draw();
  }

  function clearCardTimers() {
    if (cardExpiry) { clearTimeout(cardExpiry); cardExpiry = null; }
    if (cardTimer) { clearTimeout(cardTimer); cardTimer = null; }
  }

  function showCard(type, payload, durationMs, priority) {
    if (cardShowing && priority < cardPriority) return;

    // A now_playing push always updates the home rail chip -- the chip is
    // the resting form of the music state (owner home model). While a
    // conversation is on screen the full card is the hero (chat template);
    // while idle the chip is ALL the music gets, so return without touching
    // the overlay: music does not displace the home.
    if (type === 'now_playing') {
      applyRailNp(payload);
      if (uiState === 'idle') {
        if (cardShowing && cardType === 'now_playing') collapseNpOverlay();
        absorbIdleNp(payload);
        return;
      }
    }

    // A playing track is re-pushed every few seconds so the bar can move.
    // Re-rendering the card for that would reload the album art and restart
    // the fade several times a minute, which reads as a flicker; the same
    // track simply updates in place.
    if (type === 'now_playing' && cardShowing && cardType === 'now_playing' &&
        nowPlayingKey(payload) === cardKey) {
      applyNowPlaying(payload);
      if (cardExpiry) { clearTimeout(cardExpiry); cardExpiry = null; }
      if (durationMs > 0) cardExpiry = setTimeout(function () { hideCard(); }, durationMs);
      return;
    }

    clearCardTimers();
    cardPriority = priority;
    cardType = type;
    cardKey = type === 'now_playing' ? nowPlayingKey(payload) : null;

    if (type === 'html') {
      // Server-authored markup runs in a sandboxed iframe with no
      // allow-same-origin, so it gets an opaque origin: it can render and run
      // its own script but cannot reach this document, and therefore cannot
      // reach EchoNative. The native PushSurface got this for free by being a
      // separate WebView with no bridge on it; hosting the card in the
      // ambient page would otherwise hand a pushed <script> the tap and
      // alarm bridge.
      var frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('srcdoc', String(payload.html || ''));
      cardBody.innerHTML = '';
      cardBody.appendChild(frame);
    } else {
      var html = renderCard(type, payload);
      if (html === null) { NATIVE.log('unrenderable display type ' + type); return; }
      cardBody.innerHTML = html;
      if (type === 'timer') startCountdown(payload.seconds);
      if (type === 'now_playing') applyNowPlaying(payload);
    }

    cardShowing = true;
    cardEl.classList.remove('hidden');
    // Next frame, so the opacity transition has a starting value to run from.
    requestAnimationFrame(function () { cardEl.classList.add('shown'); });
    if (durationMs > 0) {
      cardExpiry = setTimeout(function () { hideCard(); }, durationMs);
    }
  }

  function hideCard() {
    clearCardTimers();
    if (!cardShowing) return;
    cardShowing = false;
    cardPriority = -1e9;
    cardType = null;
    cardKey = null;
    cardEl.classList.remove('shown');
    setTimeout(function () {
      if (cardShowing) return;
      cardEl.classList.add('hidden');
      cardBody.innerHTML = '';   // let the pushed page go, RAM is not free
    }, 340);
    // A cleared speaker card means the music state is gone too.
    hideRailNp();
  }

  // ---------------------------------------------------- ambient cycle
  // Owner round-1: the resting home may, once it has sat untouched for a
  // while, briefly show a full ambient page (weather / world clock) and then
  // come home again. Home stays primary -- this never runs during a
  // conversation, while a card or panel is up, or while music is on the
  // display, and any engagement (a tap, a voice session, music arriving)
  // cancels it instantly and returns home.
  var AMBIENT_AFTER_MS = 6 * 60 * 1000;   // idle this long before a rotation
  var AMBIENT_HOLD_MS = 18 * 1000;        // ...show one page for this long
  var AMBIENT_PAGES = ['weather', 'world'];
  var idleSince = Date.now();
  var endOfConvoAt = -1e12;   // when the last conversation ended (for the
                              // music-page landing, see applyFocus)
  var ambientPage = null;
  var ambientOpenedAt = 0;
  var ambientPick = 0;
  var stopwatchRunning = false;

  /** Any engagement resets the idle clock and aborts an ambient page. */
  function touchIdle() {
    idleSince = Date.now();
    if (ambientPage) {
      openPage('home');
      ambientPage = null;
    }
  }

  function ambientDue() {
    return uiState === 'idle' && focus !== 'chat' &&
      current === 'home' && !cardShowing &&
      npChipEl.classList.contains('hidden') &&   // no music on the display
      !stopwatchRunning &&                        // a live stopwatch is activity
      Date.now() - idleSince >= AMBIENT_AFTER_MS;
  }

  /** One step of the ambient cycle, driven from the idle pump (tick). */
  function ambientStep(nowMs) {
    if (ambientPage) {
      // A rotation is showing: bring it home when its time is up. (touchIdle
      // has already aborted it early on any engagement.)
      if (nowMs - ambientOpenedAt >= AMBIENT_HOLD_MS && current === ambientPage) {
        openPage('home');
        ambientPage = null;
        idleSince = Date.now();   // a full idle stretch before the next one
      }
      return;
    }
    if (!ambientDue()) return;
    ambientPick = (ambientPick + 1) % AMBIENT_PAGES.length;
    ambientPage = AMBIENT_PAGES[ambientPick];
    ambientOpenedAt = nowMs;
    openPage(ambientPage);
  }

  // ------------------------------------------------------ conversation
  var STATE_COLOR = {
    idle: 'var(--blue)', listening: '#63b3ed', thinking: '#b794f4', speaking: '#68d391'
  };
  var STATE_LABEL = { listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking' };

  function applyUiState(state) {
    uiState = state;
    var conversing = state !== 'idle';
    document.documentElement.style.setProperty('--state', STATE_COLOR[state] || STATE_COLOR.idle);
    $('ring').classList.toggle('active', state === 'listening');
    $('convo').classList.toggle('hidden', !conversing);
    if (conversing) {
      // The home is the resting surface: a conversation returns to it the
      // instant it ends, so park any open detail page underneath the chat
      // template now rather than revealing it later (owner home model).
      // Ambient rotation is off the table while somebody is talking.
      touchIdle();
      openPage('home');
      endOfConvoAt = -1e12;
      if (!document.body.classList.contains('fx-chat')) applyFocus('chat');
      $('convo-label').textContent = STATE_LABEL[state] || '';
    } else {
      $('convo-hint').classList.add('hidden');
      // If the conversation hero was the now-playing card, fold it back into
      // the rail chip -- the full page, if any, is a local surface.
      if (cardType === 'now_playing' && cardShowing) collapseNpOverlay();
      // The conversation just ended: note when, so a layout-music push that
      // follows within a beat lands on the music page rather than a plain
      // home. Optimistic return to the engine meanwhile; the server's
      // layout push corrects to music when the speaker card is still up.
      endOfConvoAt = Date.now();
      touchIdle();
      if (document.body.classList.contains('fx-chat')) applyFocus('home');
      tick();          // the clock was parked; catch it up before it is seen
    }
    schedulePump();
  }

  // ---------------------------------------------------------------- pump
  // One timer for the whole idle UI. It stops entirely during a conversation:
  // nothing behind the overlay needs per-second redraws, and ticking is pure
  // CPU contention with the audio path. The compact chat clock still moves,
  // on a slow 15 s cadence so a long session never shows a frozen time.
  var pump = null;

  function schedulePump() {
    if (pump) { clearInterval(pump); pump = null; }
    if (uiState === 'idle') {
      pump = setInterval(tick, 1000);
    } else {
      pump = setInterval(function () { drawClock(new Date()); }, 15000);
    }
  }

  var lastRailMin = -1;

  function wxClockText(now) {
    var h = now.getHours() % 12; if (h === 0) h = 12;
    return DAYS_S[now.getDay()] + ', ' + MONTHS_S[now.getMonth()] + ' ' + now.getDate() +
      ' \u00B7 ' + h + ':' + pad(now.getMinutes()) + ' ' +
      (now.getHours() >= 12 ? 'PM' : 'AM');
  }

  function tick() {
    var now = new Date();
    drawClock(now);
    if (current === 'world') drawWorld();
    if (current === 'home' && now.getMinutes() !== lastRailMin) {
      lastRailMin = now.getMinutes();
      drawRailCities(now);
    }
    // The weather page doubles as an ambient rotation screen, where the home
    // clock is not visible: keep a slim time line on it current.
    var wxNow = $('wx-now');
    if (wxNow && current === 'weather') wxNow.textContent = wxClockText(now);
    if (uiState === 'idle') ambientStep(Date.now());
  }

  // ----------------------------------------------------------------- taps
  // A tap anywhere goes to the host, exactly as it did when the ambient screen
  // was a Canvas and MainActivity.onTouchEvent saw every touch. Since brief 7
  // v2 what it means -- open a session, or end the one on screen -- is decided
  // there and not here: a blank tap starts nothing (screen = UI only). UI
  // controls mark themselves data-ui and are excluded; that is the "unless a
  // page interaction consumed it" half of the rule.
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-ui]')) return;
    NATIVE.tap();
  });

  // Any tap at all is presence: reset the ambient idle clock (and abort a
  // rotation in progress). Captured so it also covers the UI controls above.
  document.addEventListener('click', function () { touchIdle(); }, true);

  // Rail tiles with data-open navigate to their full page locally (no server
  // round trip, no session); the chips below open the native schedule pages.
  document.addEventListener('click', function (e) {
    var tile = e.target.closest('[data-open]');
    if (tile) { openPage(tile.getAttribute('data-open')); return; }
  });
  $('page-back').addEventListener('click', function () { openPage('home'); });
  $('alarm-chip').addEventListener('click', function () { NATIVE.openAlarm(); });
  $('timer-chip').addEventListener('click', function () { NATIVE.openTimer(); });
  $('stopwatch-chip').addEventListener('click', function () { NATIVE.openStopwatch(); });

  // The now-playing transport row -- on the chat-hero card AND the music
  // page (owner round-1). Buttons carry data-ui, so the tap handler above has
  // already declined to treat the press as tap-to-talk -- pressing pause must
  // not also open a conversation.
  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-media]');
    if (button) { NATIVE.media(button.getAttribute('data-media')); return; }
    // Options-panel row (UI-BRIEF-16): tap sends `select {index}`, never a
    // tap/button. One answer per question: the tapped row is highlighted and
    // every row locks until the server replaces or clears the panel. Rows
    // only exist inside the chat-hero card body.
    if (cardBody.contains(e.target)) {
      var row = e.target.closest('[data-select]');
      if (row && !row.disabled) {
        var rows = cardBody.querySelectorAll('[data-select]');
        for (var i = 0; i < rows.length; i++) rows[i].disabled = true;
        row.classList.add('chosen');
        NATIVE.select(parseInt(row.getAttribute('data-select'), 10));
      }
    }
  });

  // ------------------------------------------------- native -> JS surface
  window.Echo = {
    /** online | connecting | offline -- colour only, no text (by design). */
    linkState: function (state) {
      var dot = $('conn-dot');
      dot.className = 'dot ' + (state || 'offline');
    },

    /** idle | listening | thinking | speaking. */
    uiState: function (state) { applyUiState(state); },

    /** The protocol v1.2 weather push, unchanged: {temp_c, code, is_day}. */
    weather: function (json) {
      try {
        applyWeather(typeof json === 'string' ? JSON.parse(json) : json);
      } catch (e) {
        NATIVE.log('unusable weather payload: ' + e);
      }
    },

    /** The post-answer quiet window: shows the "press mic to stop" hint. */
    quiet: function (active) {
      $('convo-hint').classList.toggle('hidden', !active);
    },

    /** Alarm and timer counts, for the two suite chips. */
    schedule: function (alarms, timers) {
      $('alarm-chip').innerHTML = 'Alarms &middot; ' + alarms;
      $('timer-chip').innerHTML = 'Timers &middot; ' + timers;
      touchIdle();
    },

    /** Stopwatch mirror for the suite chip (v1.6). */
    stopwatch: function (running, text) {
      stopwatchRunning = !!running;
      $('stopwatch-chip').innerHTML =
        stopwatchRunning ? 'Stopwatch &middot; ' + text : 'Stopwatch';
      // The running dot + ambient gating both key off this class/flag.
      $('stopwatch-chip').classList.toggle('running', stopwatchRunning);
      touchIdle();
    },

    /**
     * Layout focus (protocol v1.6, UI-BRIEF-14): home | music | chat.
     * chat renders the conversation template. music on an idle home that
     * just finished a conversation opens the full music page; otherwise
     * music = the now-playing chip on the home (owner round-1). Unknown
     * foci fall back to home.
     */
    layout: function (raw) { applyFocus(raw); },

    /** Privacy mute state (HARDWARE-BRIEF-7 v2): show the rail chip. */
    mute: function (on) {
      $('mute-chip').classList.toggle('hidden', !on);
      touchIdle();
    },

    display: function (type, payloadJson, durationMs, priority) {
      try {
        showCard(type, JSON.parse(payloadJson), durationMs || 0, priority || 0);
        touchIdle();
      } catch (e) {
        NATIVE.log('could not render ' + type + ' card: ' + e);
      }
    },

    displayClear: function () { hideCard(); touchIdle(); },

    /**
     * Rows for the pages that have no source yet (BRIEF-10). Until one
     * arrives the page keeps its placeholder state -- an ambient screen that
     * shows fake scores is worse than one that admits it has none. Data
     * pages are opened from future rail tiles (the gallery brief); they are
     * not cycled, because the clock never leaves the home.
     */
    pageData: function (page, json) {
      try {
        var data = JSON.parse(json);
        var handler = PAGE_BINDERS[page];
        if (!handler) { NATIVE.log('no binder for page ' + page); return; }
        handler(data);
      } catch (e) {
        NATIVE.log('bad pageData for ' + page + ': ' + e);
      }
    }
  };

  // --------------------------------------------------------- page binders
  // The markup is the mockup's, row for row; only the text is substituted.
  var PAGE_BINDERS = {
    scores: function (data) {
      var rows = (data.games || []).map(function (g) {
        var meta = g.live
          ? '<span class="live">&#9679; LIVE</span><br>' + escapeHtml(g.clock || '')
          : '<span class="final">' + escapeHtml(g.status || 'FINAL') + '</span>';
        return '<div class="score-card">' +
          '<div class="team"><div class="badge" style="background:' + escapeHtml(g.home_color || 'rgba(255,255,255,0.1)') + '">' +
          escapeHtml(g.home_abbr || '') + '</div><div class="team-name">' + escapeHtml(g.home || '') +
          '<span>Home</span></div></div>' +
          '<div class="score-num">' + escapeHtml(g.home_score) + '</div>' +
          '<div class="game-meta">' + meta + '</div>' +
          '<div class="score-num">' + escapeHtml(g.away_score) + '</div>' +
          '<div class="team right"><div class="team-name">' + escapeHtml(g.away || '') +
          '<span>Away</span></div><div class="badge" style="background:' + escapeHtml(g.away_color || 'rgba(255,255,255,0.1)') + '">' +
          escapeHtml(g.away_abbr || '') + '</div></div></div>';
      });
      $('scores-list').innerHTML = rows.join('') ||
        '<div class="empty-note">No games right now</div>';
    },

    flights: function (data) {
      var flights = data.flights || [];
      $('flight-list').innerHTML = flights.map(function (f) {
        return '<div class="flight-row"><div>' +
          '<div class="flight-code">' + escapeHtml(f.callsign || '') + '</div>' +
          '<div class="flight-route">' + escapeHtml(f.route || '') + '</div></div>' +
          '<div class="flight-stat">' + escapeHtml(f.altitude || '') + ' &middot; <b>' +
          escapeHtml(f.speed || '') + '</b><br>' + escapeHtml(f.distance || '') + '</div></div>';
      }).join('') || '<div class="empty-note">Nothing overhead</div>';

      var radar = document.querySelector('.radar');
      radar.classList.remove('placeholder');
      var markers = flights.map(function (f) {
        // bearing/range are already screen-space polar from the fetcher
        var r = Math.min(70, Math.max(0, (f.range || 0)));
        var a = (f.bearing || 0) * Math.PI / 180;
        var x = 75 + r * Math.sin(a), y = 75 - r * Math.cos(a);
        return '<g transform="translate(' + x.toFixed(1) + ',' + y.toFixed(1) +
          ') rotate(' + (f.heading || 0) + ')">' +
          '<path d="M0,-6 L4,4 L0,2 L-4,4 Z" fill="#4f8cff"/></g>';
      }).join('');
      $('radar-planes').innerHTML = markers;
    },

    news: function (data) {
      var top = data.headline || {};
      $('news-tag').textContent = top.tag || 'HEADLINES';
      $('news-tag').classList.toggle('placeholder-tag', !top.tag);
      $('news-headline').textContent = top.title || 'No headlines yet';
      $('news-headline').classList.remove('placeholder');
      $('news-meta').textContent = [top.source, top.age].filter(Boolean).join(' · ');
      var ticker = $('news-ticker');
      ticker.classList.remove('placeholder');
      ticker.innerHTML = (data.ticker || []).map(function (t) {
        return '<span>' + escapeHtml(t) + '</span>';
      }).join('');
    },

    stocks: function (data) {
      $('stocks-list').innerHTML = (data.quotes || []).map(function (q) {
        var up = (q.change_pct || 0) >= 0;
        var points = (q.spark || []).map(function (v, i, arr) {
          return (i / Math.max(1, arr.length - 1) * 70).toFixed(1) + ',' + (26 - v * 26).toFixed(1);
        }).join(' ');
        return '<div class="stock-row">' +
          '<div class="stock-id"><div class="stock-sym">' + escapeHtml(q.symbol || '') + '</div>' +
          '<div class="stock-name">' + escapeHtml(q.name || '') + '</div></div>' +
          '<svg class="stock-spark" viewBox="0 0 70 26"><polyline points="' + points +
          '" fill="none" stroke="' + (up ? '#35d399' : '#ff5c5c') + '" stroke-width="2"/></svg>' +
          '<div class="stock-price"><div class="stock-val">' + escapeHtml(q.price || '') + '</div>' +
          '<div class="stock-chg ' + (up ? 'up' : 'down') + '">' + (up ? '▲ ' : '▼ ') +
          escapeHtml(Math.abs(q.change_pct || 0).toFixed(1)) + '%</div></div></div>';
      }).join('') || '<div class="empty-note">No market data</div>';
    }
  };

  // ---------------------------------------------------------------- start
  try {
    window.World.init($('world-map'));
  } catch (e) {
    NATIVE.log('world map init failed: ' + e);
  }
  // Static transport icons for the music page (the chat-hero card builds its
  // own icons on every render).
  var musicPageEl = document.querySelector('.music-page');
  if (musicPageEl) {
    var pagePrev = musicPageEl.querySelector('[data-media="previous"]');
    var pageNext = musicPageEl.querySelector('[data-media="next"]');
    if (pagePrev) pagePrev.innerHTML = icon('previous');
    if (pageNext) pageNext.innerHTML = icon('next');
  }
  applyFocus('home');   // body already starts fx-home; keeps the invariant
  openPage('home');
  tick();
  schedulePump();
  if (window.EchoNative && window.EchoNative.ready) window.EchoNative.ready();
})();
