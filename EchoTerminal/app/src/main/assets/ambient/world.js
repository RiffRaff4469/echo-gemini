/*
 * World clock map -- Apple "solar" style dot matrix.
 *
 * The mockup drew continents as filled blobs. The owner's reference is the
 * dot-matrix look instead: land is a regular grid of round dots on pure black
 * water, day-side dots bright white, night-side dots near-black grey, with the
 * real solar terminator running between them.
 *
 * Nothing here is data-driven from the network. The coastlines are coarse
 * lon/lat polygons carried in this file (a dot grid at ~3 degrees of longitude
 * cannot express coastline detail anyway, so fidelity beyond "recognisable
 * continent" would be thrown away by the sampler), and the day/night split is
 * computed from the device clock with the same solar geometry the mockup used
 * for its terminator line.
 */
(function (global) {
  'use strict';

  // Cropped like Apple's: Antarctica and the high Arctic are all ice and no
  // cities, and dropping them buys a much wider map on a 2:1 panel.
  var LAT_TOP = 76;
  var LAT_BOTTOM = -56;

  var PITCH = 5;      // css px between dot centres
  var DOT_R = 1.7;    // css px

  var DAY = '#f2f4f7';
  var NIGHT = '#232a33';
  var TERMINATOR = 'rgba(255,209,128,0.40)';
  var CITY = '#ffb454';

  // --- coastlines --------------------------------------------------------
  // Flat [lon, lat, lon, lat, ...] rings, hand-traced at continental scale.
  var LAND = [
    // North America (Alaska round to Panama, then back up the Pacific coast)
    [-168,66, -162,70, -140,70, -125,70, -115,69, -100,68, -95,62, -85,66,
     -78,62, -70,60, -64,60, -56,52, -53,47, -66,45, -70,42, -75,37, -81,31,
     -80,25, -84,30, -90,29, -97,26, -97,20, -92,18, -88,18, -88,15, -84,10,
     -79,9, -83,8, -87,13, -95,16, -105,20, -110,24, -114,31, -117,33,
     -122,37, -124,43, -125,49, -135,57, -150,60, -160,58, -165,60],
    // South America
    [-77,8, -72,12, -62,10, -52,5, -50,0, -44,-3, -38,-6, -35,-8, -39,-16,
     -44,-23, -48,-25, -54,-34, -58,-38, -62,-40, -65,-45, -68,-50, -68,-54,
     -74,-52, -75,-46, -73,-40, -71,-33, -70,-23, -71,-18, -77,-12, -81,-6,
     -80,0, -78,2],
    // Eurasia: Iberia north to the Arctic, east to the Pacific, back along
    // south Asia and Arabia to the Mediterranean.
    [-9,36, -9,43, -2,43, -4,48, 2,51, 4,53, 8,54, 8,58, 5,62, 11,65, 15,69,
     25,71, 33,70, 40,66, 44,68, 60,70, 69,73, 80,74, 100,78, 113,74, 130,73,
     140,72, 160,70, 172,66, 180,65, 180,60, 170,60, 162,60, 155,55, 140,55,
     135,48, 130,42, 126,38, 122,40, 120,34, 122,30, 117,24, 110,21, 108,16,
     106,10, 100,6, 97,16, 90,22, 85,20, 80,13, 77,8, 73,18, 69,23, 61,25,
     57,25, 56,26, 50,29, 48,30, 51,25, 56,24, 59,22, 55,17, 52,15, 45,13,
     43,16, 39,21, 36,26, 34,28, 34,31, 35,33, 36,36, 31,36, 27,37, 23,38,
     20,40, 16,38, 12,44, 5,43, 0,39, -6,36],
    // Africa
    [-17,15, -16,21, -10,26, -5,31, 10,37, 20,32, 32,31, 35,28, 43,12, 51,11,
     41,-2, 40,-10, 35,-20, 32,-28, 25,-34, 18,-34, 12,-17, 9,-1, 9,4, 3,6,
     -8,4, -13,9],
    // Australia
    [114,-22, 113,-26, 115,-34, 123,-34, 129,-32, 135,-35, 138,-35, 141,-38,
     146,-39, 150,-37, 153,-30, 146,-19, 145,-15, 142,-11, 137,-12, 130,-12,
     127,-14, 122,-17, 117,-20],
    // Greenland
    [-45,60, -52,66, -55,70, -60,76, -50,82, -30,83, -22,78, -25,72, -38,66],
    // Great Britain, Ireland, Iceland
    [-5,50, -5,55, -3,58, -2,57, 0,54, 1,52, -4,51],
    [-10,52, -10,55, -6,55, -6,52],
    [-24,65, -22,66.5, -15,66, -14,65, -18,63.5, -22,64],
    // Japan
    [130,32, 132,34, 136,35, 140,36, 141,41, 145,44, 142,45, 140,40, 137,37,
     133,35, 130,34, 129,33],
    // Madagascar, Sri Lanka, Tasmania, New Zealand
    [43,-12, 50,-15, 50,-25, 45,-25, 43,-20],
    [80,9, 82,7, 81,6, 80,7],
    [145,-41, 148,-41, 148,-43, 145,-43],
    [173,-35, 178,-38, 177,-40, 174,-41, 171,-44, 168,-47, 166,-45, 172,-41],
    // Maritime southeast Asia
    [95,5, 99,3, 106,-6, 103,-6, 96,2],                     // Sumatra
    [105,-6, 114,-8, 114,-8.6, 105,-7],                     // Java
    [109,2, 117,4, 119,-1, 116,-4, 110,-3],                 // Borneo
    [119,-5, 120,0, 125,1, 123,-2, 121,-5],                 // Sulawesi
    [131,-1, 141,-3, 147,-6, 150,-10, 143,-9, 137,-8, 131,-4], // New Guinea
    [121,18, 124,13, 126,7, 122,6, 120,13],                 // Philippines
    // Caribbean
    [-84,22, -78,23, -74,20, -80,21]
  ];

  // Bounding boxes so the point-in-land test skips 90% of the rings.
  var BOXES = LAND.map(function (ring) {
    var minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
    for (var i = 0; i < ring.length; i += 2) {
      if (ring[i] < minX) minX = ring[i];
      if (ring[i] > maxX) maxX = ring[i];
      if (ring[i + 1] < minY) minY = ring[i + 1];
      if (ring[i + 1] > maxY) maxY = ring[i + 1];
    }
    return [minX, maxX, minY, maxY];
  });

  function inRing(ring, lon, lat) {
    var inside = false;
    var n = ring.length / 2;
    for (var i = 0, j = n - 1; i < n; j = i++) {
      var xi = ring[i * 2], yi = ring[i * 2 + 1];
      var xj = ring[j * 2], yj = ring[j * 2 + 1];
      if ((yi > lat) !== (yj > lat) &&
          lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  function isLand(lon, lat) {
    for (var k = 0; k < LAND.length; k++) {
      var b = BOXES[k];
      if (lon < b[0] || lon > b[1] || lat < b[2] || lat > b[3]) continue;
      if (inRing(LAND[k], lon, lat)) return true;
    }
    return false;
  }

  // --- solar geometry ----------------------------------------------------
  // Same approximation as the mockup: declination from day-of-year, subsolar
  // longitude from UTC. Good to about a degree, which is far below one dot.
  function sun(now) {
    var start = Date.UTC(now.getUTCFullYear(), 0, 0);
    var dayOfYear = Math.floor((now.getTime() - start) / 86400000);
    var utcHours = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
    var decDeg = 23.44 * Math.sin((2 * Math.PI / 365) * (284 + dayOfYear));
    var subsolarLon = 180 - utcHours * 15;
    subsolarLon = ((subsolarLon + 180) % 360 + 360) % 360 - 180;
    return { dec: decDeg * Math.PI / 180, subsolar: subsolarLon };
  }

  // --- the map -----------------------------------------------------------
  var canvas = null, ctx = null;
  var w = 0, h = 0, dpr = 1;
  // The projected map inside the canvas. It keeps the equirectangular aspect
  // of the cropped latitude band whatever shape the box is, and the leftover
  // is left black -- which is exactly what the ocean already looks like, so
  // the letterboxing is invisible rather than merely tolerable.
  var mapW = 0, mapH = 0, offX = 0, offY = 0;
  var dots = null;   // Float32Array of x, y, lon, lat

  function project(lon, lat) {
    return {
      x: offX + (lon + 180) / 360 * mapW,
      y: offY + (LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM) * mapH
    };
  }

  function fit() {
    var aspect = 360 / (LAT_TOP - LAT_BOTTOM);
    if (w / h > aspect) {          // box is wider than the map: pillarbox
      mapH = h; mapW = h * aspect;
    } else {                       // box is taller: letterbox
      mapW = w; mapH = w / aspect;
    }
    offX = (w - mapW) / 2;
    offY = (h - mapH) / 2;
  }

  function buildDots() {
    var out = [];
    for (var y = PITCH / 2; y < mapH; y += PITCH) {
      var lat = LAT_TOP - (y / mapH) * (LAT_TOP - LAT_BOTTOM);
      for (var x = PITCH / 2; x < mapW; x += PITCH) {
        var lon = (x / mapW) * 360 - 180;
        if (isLand(lon, lat)) out.push(offX + x, offY + y, lon, lat);
      }
    }
    dots = new Float32Array(out);
  }

  function init(el) {
    canvas = el;
    ctx = canvas.getContext('2d');
    resize();
  }

  function resize() {
    if (!canvas) return false;
    var rect = canvas.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    dpr = global.devicePixelRatio || 1;
    w = Math.round(rect.width);
    h = Math.round(rect.height);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    fit();
    buildDots();
    return true;
  }

  /**
   * Redraw for `now`, marking `cities` (lon/lat/name) and highlighting hero.
   * Returns false if the canvas has no size yet -- init runs while the world
   * page is still display:none, so the first real measurement happens here,
   * the first time the page is actually shown.
   */
  function render(now, cities, heroName) {
    if (!ctx) return false;
    if (!dots || !w || !h) { if (!resize()) return false; }

    var s = sun(now);
    var sinDec = Math.sin(s.dec), cosDec = Math.cos(s.dec);
    var rad = Math.PI / 180;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, w, h);

    // Two passes, one path each: 5k arcs with a fillStyle change per dot is
    // what makes this kind of map crawl on weak hardware.
    var dayPath = new Path2D();
    var nightPath = new Path2D();
    for (var i = 0; i < dots.length; i += 4) {
      var lat = dots[i + 3] * rad;
      var H = (dots[i + 2] - s.subsolar) * rad;
      var alt = Math.sin(lat) * sinDec + Math.cos(lat) * cosDec * Math.cos(H);
      var p = alt > 0 ? dayPath : nightPath;
      p.moveTo(dots[i] + DOT_R, dots[i + 1]);
      p.arc(dots[i], dots[i + 1], DOT_R, 0, Math.PI * 2);
    }
    ctx.fillStyle = NIGHT;
    ctx.fill(nightPath);
    ctx.fillStyle = DAY;
    ctx.fill(dayPath);

    drawTerminator(s);
    drawCities(cities || [], heroName);
    return true;
  }

  function drawTerminator(s) {
    var tanDec = Math.tan(s.dec);
    ctx.beginPath();
    var started = false;
    for (var lon = -180; lon <= 180; lon += 3) {
      var H = lon - s.subsolar;
      H = ((H + 180) % 360 + 360) % 360 - 180;
      var Hrad = H * Math.PI / 180;
      var latDeg;
      if (Math.abs(tanDec) < 0.0001) {
        latDeg = Math.cos(Hrad) >= 0 ? 89 : -89;
      } else {
        latDeg = Math.atan(-Math.cos(Hrad) / tanDec) * 180 / Math.PI;
      }
      if (latDeg > LAT_TOP || latDeg < LAT_BOTTOM) { started = false; continue; }
      var p = project(lon, latDeg);
      if (started) ctx.lineTo(p.x, p.y); else { ctx.moveTo(p.x, p.y); started = true; }
    }
    ctx.strokeStyle = TERMINATOR;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function drawCities(cities, heroName) {
    ctx.font = '600 10px Inter, sans-serif';
    ctx.textBaseline = 'alphabetic';
    for (var i = 0; i < cities.length; i++) {
      var c = cities[i];
      var p = project(c.lon, c.lat);
      if (p.y < offY || p.y > offY + mapH) continue;
      var hero = c.name === heroName;

      ctx.beginPath();
      ctx.arc(p.x, p.y, hero ? 3.6 : 2.8, 0, Math.PI * 2);
      ctx.fillStyle = CITY;
      ctx.shadowColor = CITY;
      ctx.shadowBlur = hero ? 10 : 6;
      ctx.fill();
      ctx.shadowBlur = 0;

      // Labels flip to the left near the right edge so they never run off,
      // and carry a dark outline: a city sitting on the day side has bright
      // white dots directly behind its name.
      var right = p.x < w - 60;
      ctx.textAlign = right ? 'left' : 'right';
      var lx = p.x + (right ? 7 : -7);
      ctx.lineWidth = 3;
      ctx.lineJoin = 'round';
      ctx.strokeStyle = 'rgba(0,0,0,0.85)';
      ctx.strokeText(c.name, lx, p.y - 5);
      ctx.fillStyle = hero ? CITY : 'rgba(242,244,247,0.85)';
      ctx.fillText(c.name, lx, p.y - 5);
    }
  }

  global.World = { init: init, resize: resize, render: render };
})(window);
