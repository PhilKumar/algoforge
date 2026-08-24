(function () {
  var THEME_KEY = 'philforge_theme';
  var APPEARANCE_KEY = 'philforge_appearance';
  var PRESETS = window.PHILFORGE_APPEARANCE_PRESETS || {};
  var THEME_META_SELECTOR = 'meta[name="theme-color"]';
  var THEME_COLORS = { dark: '#080d14', light: '#f5f8fc' };
  var FALLBACK_TINTS = [{ id: 'gold' }, { id: 'arctic' }, { id: 'magenta' }, { id: 'citrus' }, { id: 'graphite' }, { id: 'bronze' }];
  var FALLBACK_FONTS = [{ id: 'institutional', href: '' }];
  var DEFAULT_APPEARANCE = PRESETS.default || { tint: 'gold', font: 'institutional' };
  var LEGACY_TINTS = { native: 'gold', ember: 'gold', azure: 'arctic', orchid: 'magenta', crimson: 'magenta', emerald: 'citrus' };
  var LEGACY_FONTS = { forge: 'institutional', atelier: 'grotesk', exchange: 'techno', blueprint: 'techno', scribe: 'editorial' };
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

  function resolveTheme(theme) {
    var stored = normalizeTheme(theme);
    return stored || 'dark';
  }

  function applyTheme(theme, options) {
    var requested = normalizeTheme(theme);
    var resolved = resolveTheme(requested);
    var root = document.documentElement;
    var themeMeta = document.querySelector(THEME_META_SELECTOR);
    root.setAttribute('data-theme', resolved);
    root.style.colorScheme = resolved;
    if (themeMeta) themeMeta.setAttribute('content', THEME_COLORS[resolved] || THEME_COLORS.dark);
    if (options && options.persist) {
      try {
        localStorage.setItem(THEME_KEY, resolved);
      } catch (e) {}
    }
    return resolved;
  }

  function toggleTheme() {
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    return applyTheme(next, { persist: true });
  }

  function normalizeAppearance(value) {
    var state = value || {};
    if (typeof state === 'string') state = { tint: state };
    var tint = TINTS[state.tint] ? state.tint : LEGACY_TINTS[state.tint];
    var font = Object.prototype.hasOwnProperty.call(FONTS, state.font) ? state.font : LEGACY_FONTS[state.font];
    return {
      tint: TINTS[tint] ? tint : DEFAULT_APPEARANCE.tint,
      font: Object.prototype.hasOwnProperty.call(FONTS, font) ? font : DEFAULT_APPEARANCE.font
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
    root.setAttribute('data-pf-tint', state.tint);
    root.setAttribute('data-pf-font', state.font);
    loadAppearanceFont(state.font);
    if (options && options.persist) {
      try {
        localStorage.setItem(APPEARANCE_KEY, JSON.stringify(state));
      } catch (e) {}
    }
    return state;
  }

  window.pfGetStoredTheme = getStoredTheme;
  window.pfResolveTheme = resolveTheme;
  window.pfApplyTheme = applyTheme;
  window.pfToggleTheme = toggleTheme;
  window.pfGetAppearance = getStoredAppearance;
  window.pfApplyAppearance = applyAppearance;
  /* Compatibility for older callers. The branded PNGs remain untouched. */
  window.pfRetintLogos = function () {};

  applyTheme(getStoredTheme(), { persist: false });
  applyAppearance(getStoredAppearance(), { persist: false });
})();
