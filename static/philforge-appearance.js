(function (global) {
  global.PHILFORGE_APPEARANCE_PRESETS = {
    default: { tint: 'native', font: 'forge' },
    tints: [
      { id: 'native', label: 'PhilForge Default', swatch: 'swatch-native', native: true },
      { id: 'jade', label: 'Jade Mist', swatch: 'swatch-jade' },
      { id: 'cobalt', label: 'Cobalt Haze', swatch: 'swatch-cobalt' },
      { id: 'copper', label: 'Copper Sand', swatch: 'swatch-copper' },
      { id: 'fuchsia', label: 'Rose Dusk', swatch: 'swatch-fuchsia' },
      { id: 'lime', label: 'Olive Calm', swatch: 'swatch-lime' },
    ],
    fonts: [
      { id: 'forge', label: 'Forge Native', className: 'font-forge', href: '', sample: 'Aa' },
      {
        id: 'atelier',
        label: 'Grotesk Desk',
        className: 'font-atelier',
        href: 'https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap',
        sample: 'Aa',
      },
      {
        id: 'exchange',
        label: 'Terminal Tape',
        className: 'font-exchange',
        href: 'https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700;800&family=Rajdhani:wght@500;600;700&family=Roboto+Mono:wght@400;500;600;700&display=swap',
        sample: 'Aa',
      },
      {
        id: 'blueprint',
        label: 'Circuit Draft',
        className: 'font-blueprint',
        href: 'https://fonts.googleapis.com/css2?family=Exo+2:wght@400;500;600;700;800&family=Fira+Code:wght@400;500;600;700&family=Oxanium:wght@500;600;700;800&display=swap',
        sample: 'Aa',
      },
      {
        id: 'scribe',
        label: 'Editorial Serif',
        className: 'font-scribe',
        href: 'https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700;9..144,800&family=Nunito+Sans:wght@400;500;600;700;800&family=Source+Code+Pro:wght@400;500;600;700&display=swap',
        sample: 'Aa',
      },
    ],
  };
})(window);
