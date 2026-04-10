const state = {
  items: [],
  categories: [],
  selectedId: null,
  kindFilter: 'all',
  categoryFilter: 'all',
};

let destroyPreviewAudioMotion = () => {};

function cleanupPreviewAudioMotion() {
  destroyPreviewAudioMotion();
  destroyPreviewAudioMotion = () => {};
}

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function setThemeToggleIcon() {
  const btn = $('theme-toggle');
  if (!btn) return;
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  btn.innerHTML = isLight
    ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>'
    : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2"></path><path d="M12 20v2"></path><path d="m4.93 4.93 1.41 1.41"></path><path d="m17.66 17.66 1.41 1.41"></path><path d="M2 12h2"></path><path d="M20 12h2"></path><path d="m6.34 17.66-1.41 1.41"></path><path d="m19.07 4.93-1.41 1.41"></path></svg>';
}

function toggleTheme() {
  const root = document.documentElement;
  const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  if (next === 'dark') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', 'light');
  try {
    localStorage.setItem('philforge_theme', next);
  } catch (e) {}
  setThemeToggleIcon();
}

function applyFeatured(item) {
  $('featured-title').textContent = item?.title || 'No study assets yet';
  $('featured-kind').textContent = item?.kind_label || 'Study Asset';
  $('featured-description').textContent = item?.description || 'Drop videos, decks, and audio briefs into the study folders to populate this page.';
  $('featured-category').textContent = item?.category || 'General';
  $('featured-date').textContent = item?.modified_label || '--';
  $('featured-size').textContent = item?.size_label || '--';
  const openBtn = $('open-item-btn');
  const downloadBtn = $('download-item-btn');
  openBtn.href = item?.url || '#';
  downloadBtn.href = item?.download_url || '#';
  openBtn.toggleAttribute('aria-disabled', !item);
  downloadBtn.toggleAttribute('aria-disabled', !item);
}

function bindAudioOrbMotion(shell) {
  const audio = shell.querySelector('audio');
  const orb = shell.querySelector('.audio-orb');
  const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  if (!audio || !orb || !AudioContextCtor) return;

  let audioContext = null;
  let analyser = null;
  let source = null;
  let samples = null;
  let rafId = 0;
  let destroyed = false;
  let smoothedLevel = 0;

  const stopLoop = () => {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
  };

  const resetOrb = () => {
    smoothedLevel = 0;
    orb.style.setProperty('--orb-scale', '1');
    orb.style.setProperty('--orb-glow-scale', '1');
    orb.classList.remove('is-reactive');
  };

  const applyLevel = (level) => {
    smoothedLevel += (level - smoothedLevel) * 0.24;
    orb.style.setProperty('--orb-scale', (1 + smoothedLevel * 0.28).toFixed(3));
    orb.style.setProperty('--orb-glow-scale', (1 + smoothedLevel * 0.58).toFixed(3));
    orb.classList.toggle('is-reactive', smoothedLevel > 0.05 && !audio.paused && !audio.ended);
  };

  const ensureContext = async () => {
    if (audioContext) return audioContext;
    audioContext = new AudioContextCtor();
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.82;
    samples = new Uint8Array(analyser.fftSize);
    source = audioContext.createMediaElementSource(audio);
    source.connect(analyser);
    analyser.connect(audioContext.destination);
    return audioContext;
  };

  const tick = () => {
    if (destroyed) return;
    if (audio.paused || audio.ended) {
      stopLoop();
      resetOrb();
      return;
    }

    analyser.getByteTimeDomainData(samples);
    let sumSquares = 0;
    for (let i = 0; i < samples.length; i += 1) {
      const centered = (samples[i] - 128) / 128;
      sumSquares += centered * centered;
    }
    const rms = Math.sqrt(sumSquares / samples.length);
    const level = Math.min(1, Math.max(0, (rms - 0.015) * 8));
    applyLevel(level);
    rafId = requestAnimationFrame(tick);
  };

  const start = async () => {
    try {
      const context = await ensureContext();
      if (context.state === 'suspended') await context.resume();
      if (!rafId) tick();
    } catch (err) {
      console.warn('Audio orb visualizer unavailable', err);
    }
  };

  const stop = () => {
    stopLoop();
    resetOrb();
  };

  audio.addEventListener('play', start);
  audio.addEventListener('playing', start);
  audio.addEventListener('pause', stop);
  audio.addEventListener('ended', stop);
  audio.addEventListener('emptied', stop);

  if (!audio.paused) start();
  else resetOrb();

  destroyPreviewAudioMotion = () => {
    destroyed = true;
    stopLoop();
    audio.removeEventListener('play', start);
    audio.removeEventListener('playing', start);
    audio.removeEventListener('pause', stop);
    audio.removeEventListener('ended', stop);
    audio.removeEventListener('emptied', stop);
    resetOrb();
    try {
      source?.disconnect();
    } catch (err) {}
    try {
      analyser?.disconnect();
    } catch (err) {}
    if (audioContext && audioContext.state !== 'closed') {
      audioContext.close().catch(() => {});
    }
  };
}

function renderPreview(item) {
  $('preview-title').textContent = item?.title || 'Select a deck, video, or audio brief';
  $('preview-kind').textContent = item?.kind_label || 'Preview';
  const shell = $('preview-shell');
  cleanupPreviewAudioMotion();
  if (!item) {
    shell.innerHTML = '<div class="preview-empty">Your selected deck, video, or audio brief will open here.</div>';
    return;
  }
  if (item.kind === 'video') {
    shell.innerHTML = `<video controls playsinline preload="metadata" src="${escapeHtml(item.preview_url)}"></video>`;
    return;
  }
  if (item.kind === 'deck') {
    const pdfUrl = `${item.preview_url}#toolbar=0&navpanes=0&scrollbar=0&view=FitH`;
    shell.innerHTML = `<iframe src="${escapeHtml(pdfUrl)}" title="${escapeHtml(item.title)}"></iframe>`;
    return;
  }
  if (item.kind === 'image') {
    shell.innerHTML = `<img src="${escapeHtml(item.preview_url)}" alt="${escapeHtml(item.title)}">`;
    return;
  }
  if (item.kind === 'audio') {
    shell.innerHTML = `
      <div class="audio-stage">
        <div class="audio-orb"></div>
        <div class="audio-copy">
          <span class="eyebrow">Audio Brief</span>
          <h3>${escapeHtml(item.title)}</h3>
          <p>${escapeHtml(item.description)}</p>
        </div>
        <audio controls preload="metadata" src="${escapeHtml(item.preview_url)}"></audio>
      </div>
    `;
    bindAudioOrbMotion(shell);
    return;
  }
  shell.innerHTML = `<div class="preview-empty">This asset can be downloaded from the library card.</div>`;
}

function filteredItems() {
  return state.items.filter((item) => {
    if (state.kindFilter !== 'all' && item.kind !== state.kindFilter) return false;
    if (state.categoryFilter !== 'all' && item.category !== state.categoryFilter) return false;
    return true;
  });
}

function renderTypeFilters() {
  const container = $('type-filters');
  const sourceItems = state.items.filter((item) => {
    if (state.categoryFilter !== 'all' && item.category !== state.categoryFilter) return false;
    return true;
  });
  const counts = {
    all: sourceItems.length,
    video: sourceItems.filter((item) => item.kind === 'video').length,
    deck: sourceItems.filter((item) => item.kind === 'deck').length,
    audio: sourceItems.filter((item) => item.kind === 'audio').length,
    image: sourceItems.filter((item) => item.kind === 'image').length,
  };
  const labels = {
    all: 'All Assets',
    video: 'Videos',
    deck: 'Decks',
    audio: 'Audio',
    image: 'Boards',
  };
  container.innerHTML = Object.entries(labels).map(([kind, label]) => `
    <button class="filter-chip ${state.kindFilter === kind ? 'active' : ''}" type="button" data-kind="${kind}">
      ${escapeHtml(label)} <span class="chip-count">${counts[kind] || 0}</span>
    </button>
  `).join('');
  container.querySelectorAll('[data-kind]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.kindFilter = btn.dataset.kind || 'all';
      renderLibrary();
    });
  });
}

function renderCategoryFilters() {
  const container = $('category-filters');
  const sourceItems = state.items.filter((item) => {
    if (state.kindFilter !== 'all' && item.kind !== state.kindFilter) return false;
    return true;
  });
  const counts = sourceItems.reduce((map, item) => {
    map.set(item.category, (map.get(item.category) || 0) + 1);
    return map;
  }, new Map());
  const options = [{ name: 'all', label: 'All Tracks', count: sourceItems.length }]
    .concat(state.categories
      .filter((category) => counts.has(category.name))
      .map((category) => ({
        name: category.name,
        label: category.name,
        count: counts.get(category.name) || 0,
      })));

  if (!options.some((option) => option.name === state.categoryFilter)) {
    state.categoryFilter = 'all';
  }

  container.innerHTML = `
    <label class="track-filter-control" for="category-filter-select">
      <span class="track-filter-label">Track</span>
      <span class="track-filter-select-wrap">
        <select class="track-filter-select" id="category-filter-select" aria-label="Filter study assets by track">
          ${options.map((option) => `
            <option value="${escapeHtml(option.name)}" ${option.name === state.categoryFilter ? 'selected' : ''}>
              ${escapeHtml(option.label)} (${option.count})
            </option>
          `).join('')}
        </select>
        <span class="track-filter-caret" aria-hidden="true"></span>
      </span>
    </label>
  `;

  const select = $('category-filter-select');
  select?.addEventListener('change', () => {
    state.categoryFilter = select.value || 'all';
    renderLibrary();
  });
}

function renderLibrary() {
  renderCategoryFilters();
  renderTypeFilters();
  const items = filteredItems();
  const selected = items.find((item) => item.id === state.selectedId) || items[0] || null;
  state.selectedId = selected?.id || null;
  applyFeatured(selected || state.items[0] || null);
  renderPreview(selected);

  const grid = $('library-grid');
  if (!items.length) {
    grid.innerHTML = '<div class="library-empty">No study assets match the current filter.</div>';
    return;
  }

  grid.innerHTML = items.map((item) => `
    <article class="study-card ${item.id === state.selectedId ? 'active' : ''}" data-id="${escapeHtml(item.id)}">
      <div class="study-card-top">
        <div>
          <span class="type-pill ${escapeHtml(item.accent)}">${escapeHtml(item.kind_label)}</span>
          <h3>${escapeHtml(item.title)}</h3>
          <div class="meta-text">${escapeHtml(item.category)}</div>
        </div>
        <div class="meta-text">${escapeHtml(item.modified_label)}</div>
      </div>
      <p>${escapeHtml(item.description)}</p>
      <div class="study-card-footer">
        <span>${escapeHtml(item.size_label)}</span>
        <span>${escapeHtml(item.filename)}</span>
      </div>
    </article>
  `).join('');

  grid.querySelectorAll('.study-card').forEach((card) => {
    card.addEventListener('click', () => {
      state.selectedId = card.dataset.id;
      renderLibrary();
      if (window.innerWidth <= 1100) {
        $('preview-shell')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });
}

async function loadLibrary() {
  try {
    const res = await fetch(`/api/study-library?_=${Date.now()}`, { cache: 'no-store' });
    const data = await res.json();
    state.items = Array.isArray(data.items) ? data.items : [];
    state.categories = Array.isArray(data.categories) ? data.categories : [];
    state.selectedId = data.featured?.id || state.items[0]?.id || null;
    $('library-status').textContent = state.items.length ? 'Library ready' : 'No study assets yet';
    $('hero-total').textContent = `${data.stats?.total_items || 0} assets`;
    $('hero-categories').textContent = `${data.stats?.categories || 0} categories`;
    renderLibrary();
  } catch (err) {
    console.error('Study library load failed', err);
    $('library-status').textContent = 'Library unavailable';
    $('library-grid').innerHTML = '<div class="library-empty">Failed to load the study library.</div>';
    applyFeatured(null);
    renderPreview(null);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('theme-toggle')?.addEventListener('click', toggleTheme);
  setThemeToggleIcon();
  loadLibrary();
});
