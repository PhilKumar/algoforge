(function () {
  var THEME_KEY = 'philforge_theme';
  var APPEARANCE_KEY = 'philforge_appearance';
  var DEFAULT_APPEARANCE = { tint: 'native', font: 'forge' };
  var TINTS = { native: true, jade: true, cobalt: true, copper: true, fuchsia: true, lime: true };
  var FONTS = {
    forge: '',
    atelier: 'https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap',
    exchange: 'https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700;800&family=Rajdhani:wght@500;600;700&family=Roboto+Mono:wght@400;500;600;700&display=swap',
    blueprint: 'https://fonts.googleapis.com/css2?family=Exo+2:wght@400;500;600;700;800&family=Fira+Code:wght@400;500;600;700&family=Oxanium:wght@500;600;700;800&display=swap',
    scribe: 'https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700;9..144,800&family=Nunito+Sans:wght@400;500;600;700;800&family=Source+Code+Pro:wght@400;500;600;700&display=swap'
  };

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
