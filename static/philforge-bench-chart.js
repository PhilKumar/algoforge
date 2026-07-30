/* Test Bench chart — the CryptoForge Canvas renderer, ported.
 *
 * This is the chart Phil accepted on CryptoForge on 2026-07-30, moved across
 * rather than rewritten, because a second implementation of the same drawing
 * is a second set of bugs.  What changed in the move:
 *
 *   * names are prefixed _pfChart instead of _cfChart, so nothing collides
 *     with philforge-app.js;
 *   * money is rupees, not dollars;
 *   * timestamps read as IST, which is the only clock this app uses;
 *   * the CryptoForge cascade modal's restaging helper is gone — the Test
 *     Bench owns its own page chrome and never moves the canvas host.
 *
 * The drawing itself — projection, layers, viewport, axis dragging, crosshair —
 * is untouched.  Fixes belong upstream first and then here, in that order.
 *
 * Payload shape (built by engine/test_bench.py):
 *   { timeframe, candles:[{t,o,h,l,c,is_mother}], mother:{high,low},
 *     trendlines:[{id,a1:{t,p},a2:{t,p},active}],
 *     legs:[{leg_id,touch_timestamp,touch_high,low,levels:{...},
 *            orders:[{level,inr_notional}]}],
 *     entries:[{t,price}], exits:[{t,price,pnl}],
 *     avg_entry_price, tp_price, frozen }
 * Every `t` is epoch SECONDS, never an ISO string — the projection does
 * arithmetic on them.
 */

function _pfChartIst(ts) {
  if (!ts) return '--';
  var d = new Date((Number(ts) + 19800) * 1000);
  if (isNaN(d.getTime())) return '--';
  return d.toISOString().slice(5, 16).replace('T', ' ');
}

// Indian grouping, so 1275000 reads 12,75,000 the way every other rupee figure
// in this app does.
function _pfChartInr(value) {
  var n = Number(value);
  if (!isFinite(n)) return '₹0';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}

function _pfBenchChartHostHtml() {
  return '<div class="pf-bench-canvas-host" id="pf-bench-canvas-host">'
    + '<canvas id="pf-bench-canvas-main"></canvas>'
    + '<canvas id="pf-bench-canvas-overlay"></canvas>'
    + '</div>';
}

var _PF_CHART_MAX_STRUCTURES = 3;

// buyMark / sellMark are deliberately OFF the candle palette. Green buy arrows
// vanished against green candles and red sell arrows against red ones — the two
// marks you most need to find were camouflaged by the bars they sat on. Amber
// and white appear nowhere else on the chart.
var _PF_CHART_DARK = {
  grid: 'rgba(148,163,184,0.12)', axis: 'rgba(148,163,184,0.55)',
  up: '#3fae56', down: '#d9534f', mother: '#a855f7', tp: '#10b981',
  avg: '#e2e8f0', fill: '#22c55e', fillRing: '#0b1220',
  buyMark: '#ffffff', sellMark: '#fbbf24', markRing: '#0b1220',
  fibs: ['#3b82f6', '#22c55e', '#ef4444']
};
var _PF_CHART_LIGHT = {
  grid: 'rgba(15,23,42,0.10)', axis: 'rgba(51,65,85,0.75)',
  up: '#0f766e', down: '#be123c', mother: '#7c3aed', tp: '#047857',
  avg: '#334155', fill: '#15803d', fillRing: '#ffffff',
  buyMark: '#1e293b', sellMark: '#b45309', markRing: '#ffffff',
  fibs: ['#1d4ed8', '#15803d', '#be123c']
};

function _pfChartPalette() {
  var theme = document.documentElement.getAttribute('data-theme');
  if (!theme || theme === 'auto') {
    theme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  return theme === 'light' ? _PF_CHART_LIGHT : _PF_CHART_DARK;
}


// Live canvas state, or null when no canvas chart is mounted. Holding the
// ResizeObserver here is what makes teardown possible — without it, every
// refresh would leave another observer watching a detached element.
var _pfChartCanvas = null;

function _pfChartCanvasMount(d) {
  _pfChartCanvasTeardown();
  var host = document.getElementById('pf-bench-canvas-host');
  var main = document.getElementById('pf-bench-canvas-main');
  var overlay = document.getElementById('pf-bench-canvas-overlay');
  if (!host || !main || !overlay) return;
  _pfChartCanvas = {
    host: host, main: main, overlay: overlay,
    ctx: main.getContext('2d'), octx: overlay.getContext('2d'),
    data: d, w: 0, h: 0, dpr: 0, ro: null, themeObserver: null,
    // Phase 2 owns this model. Phase 3 changes it through pan/zoom/axis-drag;
    // Phase 4 preserves it across a live refresh.
    viewport: null, projection: null, paint: null, paintKey: '', handlers: [], drag: null
  };
  var c = _pfChartCanvas;
  _pfChartCanvasResize();
  _pfChartCanvasBindInteraction(_pfChartCanvas);
  // Canvas pixels do not inherit CSS colours. Watch the single theme attribute
  // and repaint from the same payload/viewport when it changes; SVG gets this
  // for free through a fresh DOM render, Canvas must do it explicitly.
  if (window.MutationObserver) {
    _pfChartCanvas.themeObserver = new MutationObserver(function () {
      if (_pfChartCanvas && _pfChartCanvas === c) _pfChartCanvasDraw();
    });
    _pfChartCanvas.themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }
  // The host is sized entirely by CSS, and the things that change it — going
  // fullscreen, rotating a phone, dragging the window edge — do not all fire a
  // resize event on window. Observe the element itself.
  if (window.ResizeObserver) {
    _pfChartCanvas.ro = new ResizeObserver(function () { _pfChartCanvasResize(); });
    _pfChartCanvas.ro.observe(host);
  } else {
    window.addEventListener('resize', _pfChartCanvasResize);
  }
}

function _pfChartCanvasTeardown() {
  var c = _pfChartCanvas;
  _pfChartCanvas = null;
  if (!c) return;
  (c.handlers || []).forEach(function (row) { c.host.removeEventListener(row[0], row[1], row[2]); });
  if (c.ro) { try { c.ro.disconnect(); } catch (err) {} }
  else window.removeEventListener('resize', _pfChartCanvasResize);
  if (c.themeObserver) { try { c.themeObserver.disconnect(); } catch (err) {} }
}

// The actual "high definition" fix. The backing store is sized in DEVICE
// pixels and the context scaled by the ratio, so a 1px line is one real pixel
// on a retina screen. The SVG cannot do this: it is laid out once at a fixed
// 1440×660 and then stretched to whatever width the panel happens to be.
function _pfChartCanvasResize() {
  var c = _pfChartCanvas;
  if (!c || !c.host.isConnected) return;
  var box = c.host.getBoundingClientRect();
  var cssW = Math.max(Math.round(box.width), 1);
  var cssH = Math.max(Math.round(box.height), 1);
  var dpr = Math.max(window.devicePixelRatio || 1, 1);
  if (cssW === c.w && cssH === c.h && dpr === c.dpr) return;
  c.w = cssW; c.h = cssH; c.dpr = dpr;
  [c.main, c.overlay].forEach(function (cv) {
    cv.width = Math.round(cssW * dpr);
    cv.height = Math.round(cssH * dpr);
    cv.style.width = cssW + 'px';
    cv.style.height = cssH + 'px';
  });
  // Setting width/height resets the context, so the DPR transform has to be
  // reapplied every time — after which everything downstream draws in plain
  // CSS pixels and never has to think about the ratio again.
  c.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  _pfChartCanvasDraw();
}

function _pfChartCanvasStructures(d) {
  var allLegs = Array.isArray(d.legs) ? d.legs : [];
  var legs = allLegs.slice(-_PF_CHART_MAX_STRUCTURES);
  var allTls = Array.isArray(d.trendlines) ? d.trendlines : [];
  var tls = allTls.slice(-_PF_CHART_MAX_STRUCTURES);
  // Preserve Classic's one important exception: the active line is never
  // hidden just because later, retired structures filled the three-line cap.
  var active = allTls.filter(function (tl) { return tl && tl.active; })[0];
  if (active && tls.indexOf(active) === -1) tls = [active].concat(tls).slice(-_PF_CHART_MAX_STRUCTURES);
  return { legs: legs, trendlines: tls };
}

function _pfChartCanvasBarSeconds(d, candles) {
  if (candles.length > 1) {
    var span = Number(candles[candles.length - 1].t) - Number(candles[0].t);
    if (isFinite(span) && span > 0) return span / (candles.length - 1);
  }
  var tf = String(d.timeframe || d.campaign_timeframe || '5m').toLowerCase();
  var match = tf.match(/^(\d+)(m|h|d|w)$/);
  if (!match) return 300;
  var units = { m: 60, h: 3600, d: 86400, w: 604800 };
  return Math.max(Number(match[1]) * units[match[2]], 1);
}

// Initial fit is intentionally the same price rule as Classic: candles,
// mother high, displayed leg touch highs/lows and target, then 6% breathing
// room. Price and time are stored separately so later axis dragging is real,
// independent scaling rather than a viewBox trick.
function _pfChartCanvasFit(c) {
  var d = c.data || {};
  var candles = (d.candles || []).slice();
  if (!candles.length) return null;
  var lo = Number(candles[0].l), hi = Number(candles[0].h);
  candles.forEach(function (bar) {
    lo = Math.min(lo, Number(bar.l));
    hi = Math.max(hi, Number(bar.h));
  });
  if (d.mother && d.mother.high) hi = Math.max(hi, Number(d.mother.high));
  var structures = _pfChartCanvasStructures(d);
  structures.legs.forEach(function (leg) {
    if (leg.touch_high) hi = Math.max(hi, Number(leg.touch_high));
    if (leg.low) lo = Math.min(lo, Number(leg.low));
  });
  if (d.tp_price) {
    hi = Math.max(hi, Number(d.tp_price));
    lo = Math.min(lo, Number(d.tp_price));
  }
  var priceSpan = (hi - lo) || Math.max(Math.abs(hi) * 0.02, 1);
  var padP = priceSpan * 0.06;
  var first = Number(candles[0].t), last = Number(candles[candles.length - 1].t);
  var barSec = _pfChartCanvasBarSeconds(d, candles);
  return {
    tMin: first - barSec / 2,
    tMax: last + barSec / 2,
    pMin: lo - padP,
    pMax: hi + padP
  };
}

// Only fields that can change Canvas pixels belong in this key. A refresh that
// merely changes table/status data can keep the already-painted surface; a new
// candle, level or marker asks the normal draw pipeline to repaint coherently.
function _pfChartCanvasPaintKey(d) {
  d = d || {};
  return JSON.stringify({
    candles: d.candles || [], mother: d.mother || null,
    legs: d.legs || [], trendlines: d.trendlines || [],
    fills: d.fills || [], entries: d.entries || [], exits: d.exits || [],
    avg_entry_price: d.avg_entry_price, tp_price: d.tp_price,
    tp_label: d.tp_label || '', timeframe: d.timeframe
  });
}

function _pfChartCanvasSameViewport(a, b) {
  return !!a && !!b && a.tMin === b.tMin && a.tMax === b.tMax && a.pMin === b.pMin && a.pMax === b.pMax;
}

// Captured before the chart API refresh begins. At the right edge means the
// view is following the latest bar; a panned-back research view is never
// allowed to jump just because a poll returned a newer payload.
function _pfChartCanvasRefreshState() {
  var c = _pfChartCanvas, fit = c && _pfChartCanvasFit(c);
  if (!c || !c.viewport || !fit) return null;
  var bar = _pfChartCanvasBarSeconds(c.data || {}, (c.data || {}).candles || []);
  var tolerance = Math.max(bar / 2, (c.viewport.tMax - c.viewport.tMin) * 0.001);
  return {
    viewport: { tMin: c.viewport.tMin, tMax: c.viewport.tMax, pMin: c.viewport.pMin, pMax: c.viewport.pMax },
    followRight: Math.abs(c.viewport.tMax - fit.tMax) <= tolerance
  };
}

// Update the existing two Canvas surfaces instead of destroying/recreating
// them. This retains pointer bindings and DPR backing stores, and it gives the
// renderer a chance to skip paint work when the chart-specific payload did not
// actually change.
function _pfChartCanvasRefresh(d, saved) {
  var c = _pfChartCanvas;
  if (!c || !c.host || !c.host.isConnected) return false;
  var oldKey = c.paintKey, oldViewport = c.viewport;
  c.data = d;
  var fit = _pfChartCanvasFit(c);
  if (!fit) return false;
  var next = saved && saved.viewport ? {
    tMin: saved.viewport.tMin, tMax: saved.viewport.tMax,
    pMin: saved.viewport.pMin, pMax: saved.viewport.pMax
  } : fit;
  if (saved && saved.followRight) {
    var span = saved.viewport.tMax - saved.viewport.tMin;
    next.tMax = fit.tMax;
    next.tMin = fit.tMax - span;
  }
  c.viewport = next;
  var nextKey = _pfChartCanvasPaintKey(d);
  // A changed viewport moves every projected pixel, so it must repaint as a
  // whole. With an unchanged viewport, this intentionally skips paint if none
  // of the independent draw layers received new data.
  if (!_pfChartCanvasSameViewport(oldViewport, next) || oldKey !== nextKey) _pfChartCanvasDraw();
  return true;
}

// xOf/yOf and their inverses are kept together and exposed on canvas state.
// Phase 3 uses the inverse pair for cursor-anchored zoom and the crosshair.
function _pfChartCanvasProjection(c) {
  var v = c.viewport;
  if (!v) return null;
  // Classic's 150/62/26/34 gutters live in a 1440×660 viewBox. Scale that
  // exact geometry to the real Canvas surface (rather than retaining 150 CSS
  // pixels at every width), so both engines put the same payload in the same
  // horizontal lane. The floors keep labels readable on a narrow screen.
  var padL = Math.max(86, 150 * c.w / 1440);
  var padR = Math.max(42, 62 * c.w / 1440);
  var padT = Math.max(18, 26 * c.h / 660);
  var padB = Math.max(26, 34 * c.h / 660);
  var plotW = Math.max(c.w - padL - padR, 1);
  var plotH = Math.max(c.h - padT - padB, 1);
  var tSpan = Math.max(v.tMax - v.tMin, 1);
  var pSpan = Math.max(v.pMax - v.pMin, Number.EPSILON);
  var p = {
    padL: padL, padR: padR, padT: padT, padB: padB, plotW: plotW, plotH: plotH,
    fontScale: Math.max(0.75, Math.min(1, c.w / 1440, c.h / 660)),
    xOf: function (t) { return padL + ((Number(t) - v.tMin) / tSpan) * plotW; },
    yOf: function (price) { return padT + ((v.pMax - Number(price)) / pSpan) * plotH; },
    tAt: function (x) { return v.tMin + ((Number(x) - padL) / plotW) * tSpan; },
    pAt: function (y) { return v.pMax - ((Number(y) - padT) / plotH) * pSpan; },
    inPrice: function (price) { return Number(price) >= v.pMin && Number(price) <= v.pMax; }
  };
  c.projection = p;
  return p;
}

function _pfChartCanvasText(ctx, text, x, y, color, size, align, weight, scale) {
  ctx.fillStyle = color;
  ctx.font = (weight || '400') + ' ' + ((size || 10) * (scale || 1)) + 'px monospace';
  ctx.textAlign = align || 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(text), x, y);
}

function _pfChartCanvasClip(ctx, p, draw) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(p.padL, p.padT, p.plotW, p.plotH);
  ctx.clip();
  draw();
  ctx.restore();
}

function _pfChartCanvasGridAxes(c, p, PAL) {
  var ctx = c.ctx, d = c.data || {}, candles = d.candles || [];
  var labels = 0;
  ctx.lineWidth = 1;
  for (var g = 0; g <= 4; g++) {
    var price = c.viewport.pMin + (c.viewport.pMax - c.viewport.pMin) * (g / 4);
    var y = p.yOf(price);
    ctx.strokeStyle = PAL.grid;
    ctx.beginPath(); ctx.moveTo(p.padL, y); ctx.lineTo(p.padL + p.plotW, y); ctx.stroke();
    _pfChartCanvasText(ctx, Number(price).toLocaleString('en-US', { maximumFractionDigits: 2 }),
      p.padL + p.plotW + 6, y, PAL.axis, 9.5, 'left', null, p.fontScale);
    labels++;
  }
  var ticks = Math.min(6, candles.length);
  for (var i = 0; i < ticks; i++) {
    var ci = Math.round((candles.length - 1) * (i / Math.max(ticks - 1, 1)));
    _pfChartCanvasText(ctx, _pfChartIst(candles[ci].t), p.xOf(candles[ci].t), c.h - 8,
      PAL.axis, 9.5, 'center', null, p.fontScale);
    labels++;
  }
  return labels;
}

function _pfChartCanvasCandleWidth(d, p) {
  var candles = d.candles || [];
  if (candles.length < 2) return p.plotW;
  return Math.abs(p.xOf(candles[1].t) - p.xOf(candles[0].t));
}

function _pfChartCanvasMotherColumn(c, p, PAL) {
  var ctx = c.ctx, d = c.data || {}, bodyW = Math.max(Math.min(_pfChartCanvasCandleWidth(d, p) * 0.65, 9), 1);
  _pfChartCanvasClip(ctx, p, function () {
    (d.candles || []).forEach(function (bar) {
      if (!bar.is_mother) return;
      var x = p.xOf(bar.t);
      ctx.fillStyle = PAL.mother;
      ctx.globalAlpha = 0.09;
      ctx.fillRect(x - Math.max(bodyW, 6) / 2 - 3, p.padT + 1, Math.max(bodyW, 6) + 6, p.plotH - 2);
      ctx.globalAlpha = 1;
    });
  });
}

function _pfChartCanvasCandles(c, p, PAL) {
  var ctx = c.ctx, d = c.data || {}, bodyW = Math.max(Math.min(_pfChartCanvasCandleWidth(d, p) * 0.65, 9), 1);
  var count = 0;
  _pfChartCanvasClip(ctx, p, function () {
    (d.candles || []).forEach(function (bar) {
      var x = p.xOf(bar.t), up = Number(bar.c) >= Number(bar.o), color = up ? PAL.up : PAL.down;
      ctx.strokeStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, p.yOf(bar.h)); ctx.lineTo(x, p.yOf(bar.l)); ctx.stroke();
      var top = p.yOf(Math.max(Number(bar.o), Number(bar.c)));
      var bottom = p.yOf(Math.min(Number(bar.o), Number(bar.c)));
      ctx.fillStyle = color;
      ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(bottom - top, 1));
      if (bar.is_mother) {
        ctx.strokeStyle = PAL.mother; ctx.lineWidth = 1.4;
        ctx.strokeRect(x - bodyW / 2 - 1, p.yOf(bar.h) - 1, bodyW + 2,
          Math.max(p.yOf(bar.l) - p.yOf(bar.h) + 2, 4));
        _pfChartCanvasText(ctx, 'MC', x, Math.max(p.yOf(bar.h) - 8, p.padT + 10), PAL.mother, 9.5, 'center', '700', p.fontScale);
      }
      count++;
    });
  });
  return count;
}

function _pfChartCanvasTrendlines(c, p, PAL, labels) {
  var d = c.data || {}, ctx = c.ctx, drawn = 0;
  _pfChartCanvasClip(ctx, p, function () {
    _pfChartCanvasStructures(d).trendlines.forEach(function (tl) {
      var a1 = tl.a1, a2 = tl.a2;
      if (!a1 || !a2 || Number(a2.t) === Number(a1.t)) return;
      var slope = (Number(a2.p) - Number(a1.p)) / (Number(a2.t) - Number(a1.t));
      var p0 = Number(a1.p) + slope * (c.viewport.tMin - Number(a1.t));
      var p1 = Number(a1.p) + slope * (c.viewport.tMax - Number(a1.t));
      var color = PAL.fibs[(Math.max(1, Number(tl.id) || 1) - 1) % PAL.fibs.length];
      var noFib = tl.bears_fib === false;
      ctx.strokeStyle = color; ctx.lineWidth = tl.active ? 1.3 : 0.9;
      ctx.globalAlpha = noFib ? 0.35 : (tl.active ? 0.95 : 0.5);
      ctx.setLineDash(noFib ? [6, 4] : []);
      ctx.beginPath(); ctx.moveTo(p.xOf(c.viewport.tMin), p.yOf(p0)); ctx.lineTo(p.xOf(c.viewport.tMax), p.yOf(p1)); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      if (p.inPrice(p1)) labels.push({ kind: 'right', x: p.xOf(c.viewport.tMax) - 4, y: p.yOf(p1) - 5,
        text: 'TL' + tl.id + (noFib ? ' (no fib)' : (tl.active ? ' ★' : '')), color: color });
      drawn++;
    });
  });
  return drawn;
}

function _pfChartCanvasHline(c, p, labels, price, color, text, dash, width, opacity) {
  if (!p.inPrice(price)) return false;
  var ctx = c.ctx, y = p.yOf(price);
  _pfChartCanvasClip(ctx, p, function () {
    ctx.strokeStyle = color; ctx.lineWidth = width || 0.9; ctx.globalAlpha = opacity || 1;
    ctx.setLineDash(dash || []);
    ctx.beginPath(); ctx.moveTo(p.padL, y); ctx.lineTo(p.padL + p.plotW, y); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  });
  if (text) labels.push({ kind: 'gutter', y: y, text: text, color: color });
  return true;
}

function _pfChartCanvasFibs(c, p, PAL, labels) {
  var d = c.data || {}, count = 0;
  function fmt(v) { return Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 }); }
  if (d.mother && d.mother.high) count += _pfChartCanvasHline(c, p, labels, Number(d.mother.high), PAL.mother,
    'MOTHER (' + fmt(d.mother.high) + ')', [5, 3], 1.1) ? 1 : 0;
  _pfChartCanvasStructures(d).legs.forEach(function (leg) {
    var color = PAL.fibs[(Math.max(1, Number(leg.leg_id) || 1) - 1) % PAL.fibs.length];
    count += _pfChartCanvasHline(c, p, labels, Number(leg.touch_high), color,
      '0 (' + fmt(leg.touch_high) + ')', [], 0.8, 0.4) ? 1 : 0;
    count += _pfChartCanvasHline(c, p, labels, Number(leg.low), color,
      '1 (' + fmt(leg.low) + ')', [], 0.8, 0.4) ? 1 : 0;
    [2, 4, 8].forEach(function (level) {
      var price = leg.levels ? leg.levels[String(level)] : null;
      if (price == null) return;
      var order = (leg.orders || []).find(function (o) { return o.level === level; }) || {};
      var spent = Number(order.inr_notional) || 0;
      count += _pfChartCanvasHline(c, p, labels, Number(price), color,
        level + ' (' + fmt(price) + ')' + (spent > 0 ? '  ' + _pfChartInr(spent) : ''), [], 1.1, 0.9) ? 1 : 0;
    });
    if (leg.touch_timestamp && p.inPrice(leg.touch_high)) {
      var ctx = c.ctx;
      _pfChartCanvasClip(ctx, p, function () {
        ctx.strokeStyle = color; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(p.xOf(leg.touch_timestamp), p.yOf(leg.touch_high), 3.5, 0, Math.PI * 2); ctx.stroke();
      });
    }
  });
  // Upstream labelled this line "SOLD AT" whenever the campaign had ended, which
  // is only true when the target was the thing that ended it. A mother held to
  // expiry never traded here, so the caller names the line and this draws it.
  var hit = String(d.tp_label || '') === 'TARGET HIT';
  if (d.tp_price) count += _pfChartCanvasHline(c, p, labels, Number(d.tp_price), hit ? PAL.sellMark : PAL.tp,
    (d.tp_label || 'TARGET') + ' (' + fmt(d.tp_price) + ')', [6, 3], 1.2) ? 1 : 0;
  if (d.avg_entry_price) count += _pfChartCanvasHline(c, p, labels, Number(d.avg_entry_price), PAL.avg,
    'AVG ENTRY (' + fmt(d.avg_entry_price) + ')', [4, 4], 1.1) ? 1 : 0;
  return count;
}

function _pfChartCanvasMarkers(c, p, PAL, labels) {
  var d = c.data || {}, ctx = c.ctx, count = 0;
  var entries = (d.entries && d.entries.length) ? d.entries : (d.fills || []).map(function (fill) {
    return { t: fill && fill.timestamp, price: fill && fill.price };
  });
  _pfChartCanvasClip(ctx, p, function () {
    entries.forEach(function (fill) {
      if (!fill || !fill.price || !p.inPrice(fill.price)) return;
      var x = p.xOf(fill.t), y = p.yOf(fill.price) + 10;
      ctx.fillStyle = PAL.buyMark; ctx.strokeStyle = PAL.markRing; ctx.lineWidth = 0.9;
      ctx.beginPath(); ctx.moveTo(x, y - 9); ctx.lineTo(x - 5, y); ctx.lineTo(x - 2, y); ctx.lineTo(x - 2, y + 6);
      ctx.lineTo(x + 2, y + 6); ctx.lineTo(x + 2, y); ctx.lineTo(x + 5, y); ctx.closePath(); ctx.fill(); ctx.stroke();
      count++;
    });
    (d.exits || []).forEach(function (exit) {
      if (!exit || !exit.price || !p.inPrice(exit.price)) return;
      var x = p.xOf(exit.t), y = p.yOf(exit.price) - 10;
      ctx.fillStyle = PAL.sellMark; ctx.strokeStyle = PAL.markRing; ctx.lineWidth = 0.9;
      ctx.beginPath(); ctx.moveTo(x, y + 9); ctx.lineTo(x - 5, y); ctx.lineTo(x - 2, y); ctx.lineTo(x - 2, y - 6);
      ctx.lineTo(x + 2, y - 6); ctx.lineTo(x + 2, y); ctx.lineTo(x + 5, y); ctx.closePath(); ctx.fill(); ctx.stroke();
      var pnl = Number(exit.pnl) || 0;
      labels.push({ kind: 'marker', x: x, y: y - 9, text: 'SELL ' + Number(exit.price).toLocaleString('en-US', { maximumFractionDigits: 2 })
        + '  ' + (pnl >= 0 ? '+' : '−') + _pfChartInr(Math.abs(pnl)), color: PAL.sellMark });
      count++;
    });
  });
  return count;
}

function _pfChartCanvasLabels(c, p, labels) {
  var ctx = c.ctx, slots = [], count = 0;
  labels.forEach(function (label) {
    if (label.kind === 'gutter') {
      var y = label.y;
      // Do not "clean this up" into an exact 10px shift. The 0.5px overshoot
      // is the Classic ETH-freeze fix: exact 10 can remain 9.9999999998 apart
      // in floating point and loop forever in the next collision check.
      for (var pass = 0, moved = true; moved && pass <= slots.length; pass++) {
        moved = false;
        for (var k = 0; k < slots.length; k++) {
          if (Math.abs(slots[k] - y) < 10) { y = slots[k] + 10.5; moved = true; break; }
        }
      }
      slots.push(y);
      _pfChartCanvasText(ctx, label.text, p.padL - 6, y + 3, label.color, 10, 'right', null, p.fontScale);
    } else if (label.kind === 'right') {
      _pfChartCanvasText(ctx, label.text, label.x, label.y, label.color, 9.5, 'right', null, p.fontScale);
    } else {
      _pfChartCanvasText(ctx, label.text, label.x, label.y, label.color, 9.5, 'center', null, p.fontScale);
    }
    count++;
  });
  return count;
}

function _pfChartCanvasDraw() {
  var c = _pfChartCanvas;
  if (!c || !c.ctx) return;
  var d = c.data || {}, candles = d.candles || [];
  var ctx = c.ctx, PAL = _pfChartPalette();
  ctx.clearRect(0, 0, c.w, c.h);
  if (!candles.length) return;
  if (!c.viewport) c.viewport = _pfChartCanvasFit(c);
  var p = _pfChartCanvasProjection(c);
  if (!p) return;
  var labels = [];
  // Keep these independently callable layers in this exact order. Later live
  // refreshes can redraw only changed layers without changing the model.
  var axisLabelCount = _pfChartCanvasGridAxes(c, p, PAL);
  _pfChartCanvasMotherColumn(c, p, PAL);
  var candleCount = _pfChartCanvasCandles(c, p, PAL);
  var trendlineCount = _pfChartCanvasTrendlines(c, p, PAL, labels);
  var fibCount = _pfChartCanvasFibs(c, p, PAL, labels);
  var markerCount = _pfChartCanvasMarkers(c, p, PAL, labels);
  var labelCount = _pfChartCanvasLabels(c, p, labels);
  // E2E reads this small semantic paint record in addition to real pixels. It
  // is a test seam, not payload state, and catches a canvas that draws a frame
  // but silently loses candles/labels/geometry.
  c.paint = {
    candles: candleCount, trendlines: trendlineCount, fibs: fibCount, markers: markerCount,
    labels: axisLabelCount + labelCount,
    labelTexts: labels.map(function (label) { return label.text; }),
    theme: document.documentElement.getAttribute('data-theme') || 'auto'
  };
  c.paintKey = _pfChartCanvasPaintKey(d);
}

function _pfChartCanvasPoint(c, event) {
  var p = c && c.projection;
  if (!p) return null;
  var box = c.host.getBoundingClientRect();
  if (!box.width || !box.height) return null;
  var x = event.clientX - box.left, y = event.clientY - box.top;
  return {
    x: x, y: y,
    plot: x >= p.padL && x <= p.padL + p.plotW && y >= p.padT && y <= p.padT + p.plotH,
    priceAxis: x > p.padL + p.plotW && y >= p.padT && y <= p.padT + p.plotH,
    timeAxis: y > p.padT + p.plotH && x >= p.padL && x <= p.padL + p.plotW
  };
}

function _pfChartCanvasSetViewport(c, next) {
  if (!c || !next) return;
  var v = c.viewport || {};
  var tSpan = Number(next.tMax) - Number(next.tMin);
  var pSpan = Number(next.pMax) - Number(next.pMin);
  if (!isFinite(tSpan) || !isFinite(pSpan) || tSpan <= 0 || pSpan <= 0) return;
  c.viewport = { tMin: Number(next.tMin), tMax: Number(next.tMax), pMin: Number(next.pMin), pMax: Number(next.pMax) };
  var fit = _pfChartCanvasFit(c), label = document.getElementById('pf-bench-zoom-level');
  if (fit && label) label.textContent = Math.round((fit.tMax - fit.tMin) / tSpan * 100) + '%';
  _pfChartCanvasDraw();
}

function _pfChartCanvasZoom(factor, reset, anchor) {
  var c = _pfChartCanvas;
  if (!c || !c.viewport) return;
  if (reset || factor === 0) {
    _pfChartCanvasSetViewport(c, _pfChartCanvasFit(c));
    return;
  }
  var step = Number(factor);
  if (!isFinite(step) || step <= 0) return;
  var p = c.projection || _pfChartCanvasProjection(c);
  if (!p) return;
  var oldSpan = c.viewport.tMax - c.viewport.tMin;
  var minSpan = _pfChartCanvasBarSeconds(c.data || {}, (c.data || {}).candles || []) / 2;
  var newSpan = Math.max(minSpan, Math.min(oldSpan * 60, oldSpan * step));
  var x = anchor && isFinite(anchor.x) ? anchor.x : p.padL + p.plotW / 2;
  var at = p.tAt(x), ratio = (at - c.viewport.tMin) / oldSpan;
  _pfChartCanvasSetViewport(c, {
    tMin: at - ratio * newSpan,
    tMax: at + (1 - ratio) * newSpan,
    pMin: c.viewport.pMin, pMax: c.viewport.pMax
  });
}

function _pfChartCanvasDrawCrosshair(c, point) {
  var ctx = c.octx, p = c.projection;
  if (!ctx || !p) return;
  ctx.clearRect(0, 0, c.w, c.h);
  if (!point || !point.plot) return;
  var PAL = _pfChartPalette(), price = p.pAt(point.y), when = p.tAt(point.x);
  ctx.save();
  ctx.strokeStyle = PAL.axis; ctx.lineWidth = 0.7; ctx.globalAlpha = 0.8; ctx.setLineDash([4, 3]);
  ctx.beginPath(); ctx.moveTo(p.padL, point.y); ctx.lineTo(p.padL + p.plotW, point.y); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(point.x, p.padT); ctx.lineTo(point.x, p.padT + p.plotH); ctx.stroke();
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  var priceText = Number(price).toLocaleString('en-US', { maximumFractionDigits: 2 });
  ctx.font = '9.5px monospace';
  var priceW = Math.max(ctx.measureText(priceText).width + 10, 42), timeText = _pfChartIst(Math.round(when)) + ' IST';
  var timeW = Math.max(ctx.measureText(timeText).width + 10, 84);
  ctx.fillStyle = PAL.axis;
  ctx.fillRect(p.padL + p.plotW + 2, point.y - 8, priceW, 16);
  ctx.fillRect(point.x - timeW / 2, c.h - 20, timeW, 15);
  _pfChartCanvasText(ctx, priceText, p.padL + p.plotW + 6, point.y + 0.5, PAL.fillRing, 9.5, 'left');
  _pfChartCanvasText(ctx, timeText, point.x, c.h - 12, PAL.fillRing, 9, 'center');
  ctx.restore();
}

function _pfChartCanvasClearCrosshair(c) {
  if (c && c.octx) c.octx.clearRect(0, 0, c.w, c.h);
}

// Phase 3 interaction model. Plot drag pans both axes; the axis gutters are
// deliberately separate so price stretching can never move time and vice versa.
function _pfChartCanvasBindInteraction(c) {
  if (!c || !c.host) return;
  function bind(name, fn, opts) {
    c.host.addEventListener(name, fn, opts);
    c.handlers.push([name, fn, opts]);
  }
  function endDrag(event) {
    if (!c.drag) return;
    c.drag = null;
    c.host.style.cursor = '';
    try { c.host.releasePointerCapture(event.pointerId); } catch (err) {}
  }
  bind('pointerdown', function (event) {
    var point = _pfChartCanvasPoint(c, event), v = c.viewport;
    if (!point || !v || (!point.plot && !point.priceAxis && !point.timeAxis)) return;
    c.drag = {
      kind: point.plot ? 'pan' : (point.priceAxis ? 'price' : 'time'),
      x: point.x, y: point.y,
      viewport: { tMin: v.tMin, tMax: v.tMax, pMin: v.pMin, pMax: v.pMax },
      anchorTime: c.projection.tAt(point.x), anchorPrice: c.projection.pAt(point.y)
    };
    c.host.style.cursor = point.plot ? 'grabbing' : 'ns-resize';
    if (point.timeAxis) c.host.style.cursor = 'ew-resize';
    try { c.host.setPointerCapture(event.pointerId); } catch (err) {}
    _pfChartCanvasClearCrosshair(c);
  });
  bind('pointermove', function (event) {
    var point = _pfChartCanvasPoint(c, event);
    if (!point) return;
    if (!c.drag) {
      c.host.style.cursor = point.plot ? 'crosshair' : (point.priceAxis ? 'ns-resize' : (point.timeAxis ? 'ew-resize' : ''));
      _pfChartCanvasDrawCrosshair(c, point);
      return;
    }
    var drag = c.drag, start = drag.viewport, p = c.projection;
    var tSpan = start.tMax - start.tMin, pSpan = start.pMax - start.pMin;
    if (drag.kind === 'pan') {
      var dt = -(point.x - drag.x) / p.plotW * tSpan;
      var dp = (point.y - drag.y) / p.plotH * pSpan;
      _pfChartCanvasSetViewport(c, { tMin: start.tMin + dt, tMax: start.tMax + dt, pMin: start.pMin + dp, pMax: start.pMax + dp });
    } else if (drag.kind === 'price') {
      // Pulling the price axis upward zooms in (a tighter price range); pulling
      // it downward zooms out. This is intentionally opposite to screen Y,
      // which increases downward.
      var priceSpan = pSpan * Math.exp((point.y - drag.y) / 160);
      var priceRatio = (drag.anchorPrice - start.pMin) / pSpan;
      _pfChartCanvasSetViewport(c, {
        tMin: start.tMin, tMax: start.tMax,
        pMin: drag.anchorPrice - priceRatio * priceSpan,
        pMax: drag.anchorPrice + (1 - priceRatio) * priceSpan
      });
    } else {
      var timeSpan = Math.max(_pfChartCanvasBarSeconds(c.data || {}, (c.data || {}).candles || []) / 2,
        tSpan * Math.exp((drag.x - point.x) / 160));
      var timeRatio = (drag.anchorTime - start.tMin) / tSpan;
      _pfChartCanvasSetViewport(c, {
        tMin: drag.anchorTime - timeRatio * timeSpan,
        tMax: drag.anchorTime + (1 - timeRatio) * timeSpan,
        pMin: start.pMin, pMax: start.pMax
      });
    }
  });
  bind('pointerup', endDrag);
  bind('pointercancel', endDrag);
  bind('pointerleave', function () { if (!c.drag) _pfChartCanvasClearCrosshair(c); });
  bind('wheel', function (event) {
    var point = _pfChartCanvasPoint(c, event);
    if (!point || !point.plot) return;
    event.preventDefault();
    // Time only: vertical scale is reserved for deliberate price-axis drags.
    _pfChartCanvasZoom(event.deltaY < 0 ? 1 / 1.08 : 1.08, false, point);
  }, { passive: false });
  bind('dblclick', function (event) {
    var point = _pfChartCanvasPoint(c, event), fit = _pfChartCanvasFit(c);
    if (!point || !fit || !c.viewport) return;
    if (point.priceAxis) _pfChartCanvasSetViewport(c, { tMin: c.viewport.tMin, tMax: c.viewport.tMax, pMin: fit.pMin, pMax: fit.pMax });
    else if (point.timeAxis) _pfChartCanvasSetViewport(c, { tMin: fit.tMin, tMax: fit.tMax, pMin: c.viewport.pMin, pMax: c.viewport.pMax });
    else if (point.plot) _pfChartCanvasSetViewport(c, fit);
  });
}


// ── The Test Bench's one entry point ─────────────────────────
// Render into whichever container the page hands us. Mounting is idempotent:
// a second run with a fresh result repaints the same surfaces rather than
// leaving a detached canvas and its observers behind.
function pfBenchDrawChart(container, payload) {
  if (!container) return false;
  if (!payload || !(payload.candles || []).length) {
    _pfChartCanvasTeardown();
    container.innerHTML = '<p class="pf-bench-empty">No candles to draw.</p>';
    return false;
  }
  container.innerHTML = _pfBenchChartHostHtml();
  _pfChartCanvasMount(payload);
  return true;
}
