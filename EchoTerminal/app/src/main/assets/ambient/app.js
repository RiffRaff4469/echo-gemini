/*
 * The ambient UI's only script. Everything the panel shows when it is not in
 * a conversation lives here.
 *
 * Two directions across the bridge, and they are deliberately narrow:
 *
 *   native -> JS   window.Echo.{linkState,uiState,weather,quiet,schedule,
 *                               display,displayClear,pageData}
 *   JS -> native   window.EchoNative.{tap,openAlarm,openTimer,log}
 *
 * The JS side owns no state the app cannot rebuild: if the WebView is torn
 * down and reloaded, MainActivity replays the last link state, ui state,
 * weather reading and schedule counts. Nothing here talks to the network --
 * the device has none.
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
    log: function (m) { console.log(m); }
  };

  var $ = function (id) { return document.getElementById(id); };

  function escapeHtml(raw) {
    return String(raw)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ----------------------------------------------------------------- pages
  var PAGES = [
    { id: 'home',    label: 'Home',        always: true },
    { id: 'weather', label: 'Weather',     always: true },
    { id: 'world',   label: 'World Clock', always: true },
    { id: 'scores',  label: 'Scores' },
    { id: 'flights', label: 'Overhead' },
    { id: 'news',    label: 'News' },
    { id: 'stocks',  label: 'Markets' }
  ];

  var CYCLE_MS = 15000;   // how long each page holds during the idle cycle
  var PIN_MS = 60000;     // how long a hand-picked page stays picked

  var current = 0;
  var cycleDueAt = 0;
  var pinnedUntil = 0;
  var uiState = 'idle';
  var cardShowing = false;

  var pager = $('pager');
  PAGES.forEach(function (p, i) {
    var d = document.createElement('div');
    d.className = 'pdot' + (i === 0 ? ' on' : '');
    d.setAttribute('data-ui', '');
    d.addEventListener('click', function () { pin(i); });
    pager.appendChild(d);
  });

  /** A page joins the cycle only once it has something true to show. */
  function eligible(i) {
    return PAGES[i].always === true || PAGES[i].hasData === true;
  }

  function goTo(i) {
    current = i;
    var id = PAGES[i].id;
    var pages = document.querySelectorAll('.page');
    for (var k = 0; k < pages.length; k++) {
      pages[k].classList.toggle('visible', pages[k].dataset.page === id);
    }
    var dots = pager.children;
    for (var d = 0; d < dots.length; d++) {
      dots[d].classList.toggle('on', d === i);
    }
    $('page-label').textContent = PAGES[i].label;
    if (id === 'world') drawWorld();
  }

  function pin(i) {
    pinnedUntil = Date.now() + PIN_MS;
    cycleDueAt = pinnedUntil;
    for (var d = 0; d < pager.children.length; d++) {
      pager.children[d].classList.toggle('pinned', d === i);
    }
    goTo(i);
  }

  function advance() {
    for (var step = 1; step <= PAGES.length; step++) {
      var next = (current + step) % PAGES.length;
      if (eligible(next)) { goTo(next); return; }
    }
  }

  function cycleTick(now) {
    if (uiState !== 'idle' || cardShowing) return;
    if (now < pinnedUntil) return;
    if (pinnedUntil) {
      pinnedUntil = 0;
      for (var d = 0; d < pager.children.length; d++) {
        pager.children[d].classList.remove('pinned');
      }
    }
    if (now >= cycleDueAt) {
      advance();
      cycleDueAt = now + CYCLE_MS;
    }
  }

  // ----------------------------------------------------------------- clock
  var MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];
  var DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function drawClock(now) {
    var h24 = now.getHours();
    var h12 = h24 % 12; if (h12 === 0) h12 = 12;
    $('time-digital').innerHTML =
      h12 + '<span class="colon">:</span>' + pad(now.getMinutes()) +
      '<span class="ampm">' + (h24 >= 12 ? 'PM' : 'AM') + '</span>';
    $('date-digital').textContent =
      DAYS[now.getDay()] + ', ' + MONTHS[now.getMonth()] + ' ' + now.getDate();
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
  CITIES.forEach(function (c) {
    var card = document.createElement('div');
    card.className = 'city-card';
    card.innerHTML = '<div class="cname">' + escapeHtml(c.name) + '</div>' +
                     '<div class="ctime">--:--</div><div class="cbadge"></div>';
    cityListEl.appendChild(card);
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
  // The same five display types PushSurface renders natively, with the same
  // priority and duration rules, so the server sees no behaviour change.
  var cardEl = $('card');
  var cardBody = $('card-body');
  var cardPriority = -1e9;
  var cardExpiry = null;
  var cardTimer = null;

  function cardPage(inner) { return '<div class="card-wrap">' + inner + '</div>'; }

  function renderCard(type, p) {
    if (type === 'text') {
      return cardPage('<div class="card-main">' + escapeHtml(p.text) + '</div>' +
        (p.subtitle ? '<div class="card-sub">' + escapeHtml(p.subtitle) + '</div>' : ''));
    }
    if (type === 'image') {
      return cardPage('<img class="card-img" src="' + escapeHtml(p.url) + '" alt="">');
    }
    if (type === 'now_playing') {
      var duration = Math.max(0, p.duration_s || 0);
      var progress = Math.min(Math.max(0, p.progress_s || 0), duration);
      var pct = duration > 0 ? (progress / duration * 100) : 0;
      return cardPage(
        (p.art_url ? '<img class="card-img" style="max-height:200px" src="' + escapeHtml(p.art_url) + '" alt="">' : '') +
        '<div class="card-main" style="font-size:44px">' + escapeHtml(p.title || '') + '</div>' +
        '<div class="card-sub">' + escapeHtml(p.artist || '') + ' &middot; ' + escapeHtml(p.album || '') + '</div>' +
        '<div class="card-bar"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
        '<div class="card-sub" style="font-size:18px">' +
        (p.is_playing === false ? 'Paused' : 'Now playing') + '</div>');
    }
    if (type === 'timer') {
      return cardPage('<div class="card-sub">' + escapeHtml(p.label || '') + '</div>' +
        '<div class="card-main card-count" id="card-count">--:--</div>');
    }
    return null;   // html is handled separately: it goes in a sandbox
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
    clearCardTimers();
    cardPriority = priority;

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
    cardEl.classList.remove('shown');
    setTimeout(function () {
      if (cardShowing) return;
      cardEl.classList.add('hidden');
      cardBody.innerHTML = '';   // let the pushed page go, RAM is not free
    }, 340);
    cycleDueAt = Date.now() + CYCLE_MS;
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
    pager.classList.toggle('hidden', conversing);
    if (conversing) {
      $('convo-label').textContent = STATE_LABEL[state] || '';
    } else {
      $('convo-hint').classList.add('hidden');
      cycleDueAt = Date.now() + CYCLE_MS;
      tick();          // the clock was parked; catch it up before it is seen
    }
    schedulePump();
  }

  // ---------------------------------------------------------------- pump
  // One timer for the whole UI. It stops entirely during a conversation:
  // nothing behind the overlay is visible, so ticking it is pure battery and
  // pure CPU contention with the audio path.
  var pump = null;

  function schedulePump() {
    if (pump) { clearInterval(pump); pump = null; }
    if (uiState === 'idle') pump = setInterval(tick, 1000);
  }

  function tick() {
    var now = new Date();
    drawClock(now);
    if (PAGES[current].id === 'world') drawWorld();
    cycleTick(now.getTime());
  }

  // ----------------------------------------------------------------- taps
  // A tap anywhere goes to the host, exactly as it did when the ambient screen
  // was a Canvas and MainActivity.onTouchEvent saw every touch. What it means
  // -- open a session, or end the one on screen -- is decided there and not
  // here. Page controls mark themselves data-ui and are excluded; that is the
  // "unless a page interaction consumed it" half of the rule. The conversation
  // hint is deliberately NOT one of them: it is a label, and tapping it is a
  // tap like any other.
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-ui]')) return;
    NATIVE.tap();
  });
  $('alarm-chip').addEventListener('click', function () { NATIVE.openAlarm(); });
  $('timer-chip').addEventListener('click', function () { NATIVE.openTimer(); });

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

    /** The post-answer quiet window: shows the "tap to stop" hint. */
    quiet: function (active) {
      $('convo-hint').classList.toggle('hidden', !active);
    },

    /** Alarm and timer counts, for the two home chips. */
    schedule: function (alarms, timers) {
      $('alarm-chip').innerHTML = 'Alarms &middot; ' + alarms;
      $('timer-chip').innerHTML = 'Timers &middot; ' + timers;
    },

    display: function (type, payloadJson, durationMs, priority) {
      try {
        showCard(type, JSON.parse(payloadJson), durationMs || 0, priority || 0);
      } catch (e) {
        NATIVE.log('could not render ' + type + ' card: ' + e);
      }
    },

    displayClear: function () { hideCard(); },

    /**
     * Rows for the pages that have no source yet (BRIEF-10). Until one
     * arrives the page keeps its placeholder state and is skipped by the idle
     * cycle -- an ambient screen that shows fake scores is worse than one
     * that admits it has none.
     */
    pageData: function (page, json) {
      try {
        var data = JSON.parse(json);
        var handler = PAGE_BINDERS[page];
        if (!handler) { NATIVE.log('no binder for page ' + page); return; }
        handler(data);
        for (var i = 0; i < PAGES.length; i++) {
          if (PAGES[i].id === page) PAGES[i].hasData = true;
        }
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
  goTo(0);
  cycleDueAt = Date.now() + CYCLE_MS;
  tick();
  schedulePump();
  if (window.EchoNative && window.EchoNative.ready) window.EchoNative.ready();
})();
