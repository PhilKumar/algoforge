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

  window.pfGetStoredTheme = getStoredTheme;
  window.pfApplyTheme = applyTheme;
  window.pfToggleTheme = toggleTheme;
  window.pfGetAppearance = getStoredAppearance;
  window.pfApplyAppearance = applyAppearance;

  applyTheme(getStoredTheme(), { persist: false });
  applyAppearance(getStoredAppearance(), { persist: false });
})();
