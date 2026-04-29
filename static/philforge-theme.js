(function () {
  var THEME_KEY = 'philforge_theme';
  var LEGACY_THEME_KEY = 'algoforge_theme';
  var APPEARANCE_KEY = 'philforge_appearance';
  var LEGACY_APPEARANCE_KEY = 'algoforge_appearance';
  var DEFAULT_APPEARANCE = { tint: 'jade', font: 'forge' };
  var TINTS = { jade: true, cobalt: true, copper: true, fuchsia: true, lime: true };
  var FONTS = {
    forge: '',
    atelier: 'https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@500;600;700;800&family=Geist+Mono:wght@400;500;600;700&family=Manrope:wght@400;500;600;700;800&display=swap',
    exchange: 'https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700;800&family=Inter+Tight:wght@400;500;600;700;800&family=Red+Hat+Mono:wght@400;500;600;700&display=swap',
    blueprint: 'https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Fira+Code:wght@400;500;600;700&family=Rubik:wght@400;500;600;700;800&display=swap',
    scribe: 'https://fonts.googleapis.com/css2?family=Martian+Mono:wght@400;500;600;700&family=Newsreader:opsz,wght@6..72,600;6..72,700;6..72,800&family=Source+Sans+3:wght@400;500;600;700;800&display=swap'
  };

  function normalizeTheme(value) {
    return value === 'light' || value === 'dark' ? value : '';
  }

  function getStoredTheme() {
    try {
      return normalizeTheme(localStorage.getItem(THEME_KEY) || localStorage.getItem(LEGACY_THEME_KEY));
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
        localStorage.removeItem(LEGACY_THEME_KEY);
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
      var raw = localStorage.getItem(APPEARANCE_KEY) || localStorage.getItem(LEGACY_APPEARANCE_KEY);
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
        localStorage.removeItem(LEGACY_APPEARANCE_KEY);
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
