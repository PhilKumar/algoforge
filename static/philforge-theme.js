(function () {
  var THEME_KEY = 'philforge_theme';
  var APPEARANCE_KEY = 'philforge_appearance';
  var PRESETS = window.PHILFORGE_APPEARANCE_PRESETS || {};
  var FALLBACK_TINTS = [{ id: 'native' }, { id: 'jade' }, { id: 'cobalt' }, { id: 'copper' }, { id: 'fuchsia' }, { id: 'lime' }];
  var FALLBACK_FONTS = [{ id: 'forge', href: '' }];
  var DEFAULT_APPEARANCE = PRESETS.default || { tint: 'native', font: 'forge' };
  var TINTS = {};
  var FONTS = {};
  (PRESETS.tints || FALLBACK_TINTS).forEach(function (tint) {
    if (tint && tint.id) TINTS[tint.id] = true;
  });
  (PRESETS.fonts || FALLBACK_FONTS).forEach(function (font) {
    if (font && font.id) FONTS[font.id] = font.href || '';
  });

  function normalizeTheme(value) {
    return value === 'light' || value === 'dark' ? value : '';
  }

  function getStoredTheme() {
    try {
      return normalizeTheme(localStorage.getItem(THEME_KEY));
    } catch (e) {
      return '';
    }
  }

  function applyTheme(theme, options) {
    var resolved = normalizeTheme(theme);
    var root = document.documentElement;
    if (resolved) root.setAttribute('data-theme', resolved);
    else root.removeAttribute('data-theme');
    root.style.colorScheme = resolved || 'dark';
    if (options && options.persist && resolved) {
      try {
        localStorage.setItem(THEME_KEY, resolved);
      } catch (e) {}
    }
    return resolved || 'dark';
  }

  function toggleTheme() {
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    return applyTheme(next, { persist: true });
  }

  function normalizeAppearance(value) {
    var state = value || {};
    if (typeof state === 'string') state = { tint: state };
    return {
      tint: TINTS[state.tint] ? state.tint : DEFAULT_APPEARANCE.tint,
      font: Object.prototype.hasOwnProperty.call(FONTS, state.font) ? state.font : DEFAULT_APPEARANCE.font
    };
  }

  function getStoredAppearance() {
    try {
      var raw = localStorage.getItem(APPEARANCE_KEY);
      if (!raw) return normalizeAppearance();
      return normalizeAppearance(JSON.parse(raw));
    } catch (e) {
      return normalizeAppearance();
    }
  }

  function loadAppearanceFont(font) {
    var href = FONTS[font] || '';
    var existing = document.getElementById('philforge-appearance-font');
    if (!href) {
      if (existing) existing.remove();
      return;
    }
    if (!existing) {
      existing = document.createElement('link');
      existing.id = 'philforge-appearance-font';
      existing.rel = 'stylesheet';
      document.head.appendChild(existing);
    }
    if (existing.getAttribute('href') !== href) existing.setAttribute('href', href);
  }

  function applyAppearance(next, options) {
    var current = getStoredAppearance();
    var incoming = next || {};
    if (typeof incoming === 'string') incoming = { tint: incoming };
    var state = normalizeAppearance({
      tint: incoming.tint || current.tint,
      font: incoming.font || current.font
    });
    var root = document.documentElement;
    if (state.tint === 'native') root.removeAttribute('data-pf-tint');
    else root.setAttribute('data-pf-tint', state.tint);
    root.setAttribute('data-pf-font', state.font);
    loadAppearanceFont(state.font);
    if (options && options.persist) {
      try {
        localStorage.setItem(APPEARANCE_KEY, JSON.stringify(state));
      } catch (e) {}
    }
    return state;
  }

  /* ── The logo follows the tint ─────────────────────────────────────
     The mark is a PNG, so CSS variables cannot reach inside it. A canvas
     re-colours it instead: draw the art, lay the tint over it with the
     'color' composite (keeps the artwork's own light and shade, replaces
     its hue), then clip back to the original alpha. 'native' restores
     the untouched file. Failures fall through silently to the original —
     a logo is never worth an error. */
  var LOGO_SELECTOR = 'img[src*="/static/logo"], img.theme-logo';
  var logoOriginals = new WeakMap();
  var logoCache = {};

  function tintPrimary() {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue('--pf-tint-primary').trim();
      return v || '';
    } catch (e) { return ''; }
  }

  function tintedLogoUrl(src, colour, done) {
    var key = src + '|' + colour;
    if (logoCache[key]) return done(logoCache[key]);
    var img = new Image();
    img.onload = function () {
      try {
        var canvas = document.createElement('canvas');
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        var ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);
        ctx.globalCompositeOperation = 'color';
        ctx.fillStyle = colour;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.globalCompositeOperation = 'destination-in';
        ctx.drawImage(img, 0, 0);
        logoCache[key] = canvas.toDataURL('image/png');
        done(logoCache[key]);
      } catch (e) { done(''); }
    };
    img.onerror = function () { done(''); };
    img.src = src;
  }

  function retintLogos() {
    var state = getStoredAppearance();
    var native = state.tint === 'native' || document.documentElement.getAttribute('data-pf-tint') == null;
    var colour = native ? '' : tintPrimary();
    document.querySelectorAll(LOGO_SELECTOR).forEach(function (el) {
      if (!logoOriginals.has(el)) logoOriginals.set(el, el.getAttribute('src'));
      var original = logoOriginals.get(el);
      if (!original) return;
      if (!colour) { el.setAttribute('src', original); return; }
      tintedLogoUrl(original, colour, function (url) {
        // Re-check before applying: an async load may finish after the user
        // has already picked a different tint.
        var now = getStoredAppearance();
        var expect = now.tint === 'native' ? '' : tintPrimary();
        if (url && expect === colour) el.setAttribute('src', url);
      });
    });
    // The header mark is a CSS background, not an <img>.
    document.querySelectorAll('.header-brand-logo').forEach(function (el) {
      var theme = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      var source = theme === 'light' ? '/static/logolight.png' : '/static/logo.png';
      if (!colour) { el.style.removeProperty('background-image'); return; }
      tintedLogoUrl(source, colour, function (url) {
        var now = getStoredAppearance();
        var expect = now.tint === 'native' ? '' : tintPrimary();
        if (url && expect === colour) el.style.backgroundImage = 'url(' + url + ')';
      });
    });
  }

  window.pfGetStoredTheme = getStoredTheme;
  window.pfApplyTheme = function (theme, options) {
    var out = applyTheme(theme, options);
    retintLogos();
    return out;
  };
  window.pfToggleTheme = function () {
    var out = toggleTheme();
    retintLogos();
    return out;
  };
  window.pfGetAppearance = getStoredAppearance;
  window.pfApplyAppearance = function (next, options) {
    var out = applyAppearance(next, options);
    retintLogos();
    return out;
  };
  window.pfRetintLogos = retintLogos;

  applyTheme(getStoredTheme(), { persist: false });
  applyAppearance(getStoredAppearance(), { persist: false });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', retintLogos);
  } else {
    retintLogos();
  }
})();
