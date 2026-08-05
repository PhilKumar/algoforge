(function (global) {
  global.PHILFORGE_APPEARANCE_PRESETS = {
    default: { tint: 'native', font: 'forge' },
    tints: [
      { id: 'native', label: 'PhilForge Default', swatch: 'swatch-native', native: true },
      /* Five deliberately CONTRASTING rooms — the old five (jade, cobalt,
         copper, fuchsia, lime) were all low-saturation pastels an arm's
         length apart, and Phil's verdict was "all feels same greenish".
         An old stored id normalises to native, harmlessly. */
      { id: 'ember', label: 'Ember Glow', swatch: 'swatch-ember' },
      { id: 'azure', label: 'Azure Sky', swatch: 'swatch-azure' },
      { id: 'orchid', label: 'Orchid Bloom', swatch: 'swatch-orchid' },
      { id: 'crimson', label: 'Crimson Pulse', swatch: 'swatch-crimson' },
      { id: 'emerald', label: 'Emerald Drive', swatch: 'swatch-emerald' },
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
