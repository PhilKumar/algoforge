  // forge.js — the shared front door at philforge.in/.
  //
  // The terminal used to live at "/", so old bookmarks, PWA shortcuts and
  // history entries point at /#portfolio-page and friends. Hand those straight
  // through to /app rather than dropping someone on the marketing page.
  //
  // Every terminal tab id ends in "-page" (dashboard-page, builder-page,
  // results-page, live-page, portfolio-page, stock-terminal-page,
  // options-cascade-page, scalp-page, charts-page) and results-page carries an
  // optional "/<runId>" suffix. This page's own anchors are #story, #desks and
  // #enter, so the suffix test cannot match one of them by accident.
  var hash = (window.location.hash || '').replace('#', '');
  if (/^[a-z-]+-page(\/\d+)?$/.test(hash)) {
    window.location.replace('/app#' + hash);
  }

  document.documentElement.classList.remove('no-js');

  var io = new IntersectionObserver(function(entries) {
    entries.forEach(function(e) {
      if (e.isIntersecting) { e.target.classList.add('in-view'); io.unobserve(e.target); }
    });
  }, { threshold: 0.15 });
  document.querySelectorAll('.rv').forEach(function(el) { io.observe(el); });

  // Gentle parallax on the full-bleed chapter images.
  var bands = [].slice.call(document.querySelectorAll('.band-media img'));
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (bands.length && !reduce) {
    var ticking = false;
    var apply = function() {
      var vh = window.innerHeight;
      bands.forEach(function(img) {
        var r = img.parentElement.parentElement.getBoundingClientRect();
        if (r.bottom < -200 || r.top > vh + 200) return;
        var progress = (r.top + r.height / 2 - vh / 2) / vh;   // -1 … 1
        img.style.transform = 'scale(1.12) translateY(' + (progress * -4.5).toFixed(2) + '%)';
      });
      ticking = false;
    };
    window.addEventListener('scroll', function() {
      if (!ticking) { ticking = true; requestAnimationFrame(apply); }
    }, { passive: true });
    apply();
  }
