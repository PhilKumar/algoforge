(() => {
  'use strict';

  const body = document.body;
  const source = body.dataset.documentSource;
  const platform = body.dataset.platform;
  const documentRoot = document.querySelector('#document-body');
  const tocRoot = document.querySelector('#document-toc');
  const metaRoot = document.querySelector('#document-meta');
  const searchInput = document.querySelector('#blueprint-search');
  const searchStatus = document.querySelector('#search-status');
  const usedSectionIds = new Set();
  let currentTocTopics = null;

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function safeLink(url) {
    if (url.includes('cryptoforge-philforge-product-architecture-system-blueprint.md')) return '/architecture';
    if (url.startsWith('./')) return '#';
    if (url.startsWith('/') || url.startsWith('#') || /^https:\/\//i.test(url)) return url;
    return '#';
  }

  function appendInline(parent, text) {
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
    let cursor = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith('**')) {
        const strong = make('strong');
        appendInline(strong, token.slice(2, -2));
        parent.append(strong);
      } else if (token.startsWith('`')) {
        parent.append(make('code', '', token.slice(1, -1)));
      } else {
        const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
        const link = make('a', '', parts ? parts[1] : token);
        link.href = safeLink(parts ? parts[2] : '#');
        parent.append(link);
      }
      cursor = pattern.lastIndex;
    }
    if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
  }

  function slug(value) {
    return value.toLowerCase().replace(/['’]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'section';
  }

  function tableCells(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function isTableDivider(line) {
    return /^\|?\s*:?-{3,}/.test(line.trim());
  }

  function buildTable(lines, start) {
    const headers = tableCells(lines[start]);
    const rows = [];
    let index = start + 2;
    while (index < lines.length && lines[index].trim().startsWith('|')) {
      rows.push(tableCells(lines[index]));
      index += 1;
    }
    const wrap = make('div', 'doc-table-wrap');
    const table = make('table', 'doc-table');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    headers.forEach((value) => { const th = document.createElement('th'); appendInline(th, value); headRow.append(th); });
    thead.append(headRow);
    const tbody = document.createElement('tbody');
    rows.forEach((values) => {
      const row = document.createElement('tr');
      headers.forEach((_, cellIndex) => { const td = document.createElement('td'); appendInline(td, values[cellIndex] || ''); row.append(td); });
      tbody.append(row);
    });
    table.append(thead, tbody);
    wrap.append(table);
    return { node: wrap, next: index };
  }

  function sectionHeading(section, title, level) {
    const wrap = make('div', 'doc-heading');
    const numberMatch = title.match(/^(?:Chapter\s+)?([0-9]+(?:\.[0-9]+)*)[:.]?\s*(.*)$/i);
    const number = numberMatch ? numberMatch[1] : level === 1 ? 'CH' : '§';
    const label = numberMatch ? numberMatch[2] : title;
    wrap.append(make('span', 'doc-number', number));
    wrap.append(make('h2', '', label || title));
    section.append(wrap);
  }

  function createSection(title, level, toc) {
    const section = make('section', `doc-section${level === 1 ? ' is-chapter' : ''}`);
    let id = slug(title);
    let serial = 2;
    while (usedSectionIds.has(id)) { id = `${slug(title)}-${serial}`; serial += 1; }
    usedSectionIds.add(id);
    section.id = id;
    sectionHeading(section, title, level);
    const chapterLabel = title.replace(/^Chapter\s+([0-9]+):\s*/i, 'Chapter $1 · ');
    const link = make('a', level === 1 ? 'toc-chapter-overview' : '', level === 1 ? 'Chapter overview' : title);
    link.href = `#${id}`;
    link.dataset.target = id;
    if (level === 1) {
      const group = make('details', 'toc-group');
      const summary = make('summary', 'toc-chapter', chapterLabel);
      const topics = make('div', 'toc-topics');
      topics.append(link);
      group.append(summary, topics);
      toc.append(group);
      currentTocTopics = topics;
    } else {
      (currentTocTopics || toc).append(link);
    }
    return section;
  }

  function renderMetadata(metadata) {
    const fragment = document.createDocumentFragment();
    metadata.forEach(([label, value]) => {
      const chip = make('div', 'meta-chip');
      chip.append(make('span', '', label));
      const strong = make('strong');
      appendInline(strong, value);
      chip.append(strong);
      fragment.append(chip);
    });
    metaRoot.replaceChildren(fragment);
  }

  function renderMarkdown(markdown) {
    usedSectionIds.clear();
    currentTocTopics = null;
    const lines = markdown.replace(/\r\n/g, '\n').split('\n');
    const metadata = [];
    const content = document.createDocumentFragment();
    const toc = document.createDocumentFragment();
    let section = null;
    let index = 0;

    while (index < lines.length) {
      const line = lines[index].trim();
      const meta = line.match(/^\*\*([^:]+):\*\*\s*(.*?)\s{0,2}$/);
      if (meta) metadata.push([meta[1], meta[2]]);
      index += 1;
      if (line === '---') break;
    }
    renderMetadata(metadata);

    const scopeSection = createSection('Document scope', 2, toc);
    content.append(scopeSection);
    section = scopeSection;

    index = 0;
    let skippedTitle = false;
    while (index < lines.length) {
      const raw = lines[index];
      const line = raw.trim();
      if (!line) { index += 1; continue; }

      if (line.startsWith('# ')) {
        if (!skippedTitle) { skippedTitle = true; index += 1; continue; }
        section = createSection(line.slice(2), 1, toc);
        content.append(section);
        index += 1;
        continue;
      }
      if (line.startsWith('## ')) {
        section = createSection(line.slice(3), 2, toc);
        content.append(section);
        index += 1;
        continue;
      }
      if (line.startsWith('### ')) {
        if (!section) { section = createSection('Document scope', 2, toc); content.append(section); }
        const heading = make('h3', 'doc-subheading', line.slice(4));
        heading.id = slug(line.slice(4));
        section.append(heading);
        index += 1;
        continue;
      }
      if (line === '---') { index += 1; continue; }
      if (/^\*\*.*(?:chapter|blueprint).*complete.*\*\*$/i.test(line)) {
        const note = make('p', 'completion-note');
        appendInline(note, line);
        section.append(note);
        index += 1;
        continue;
      }
      if (line.startsWith('|') && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        const result = buildTable(lines, index);
        section.append(result.node);
        index = result.next;
        continue;
      }
      if (/^-\s+/.test(line)) {
        const list = document.createElement('ul');
        while (index < lines.length && /^-\s+/.test(lines[index].trim())) {
          const item = make('li');
          appendInline(item, lines[index].trim().replace(/^-\s+/, ''));
          list.append(item);
          index += 1;
        }
        section.append(list);
        continue;
      }
      if (/^\d+\.\s+/.test(line)) {
        const list = document.createElement('ol');
        while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) {
          const item = make('li');
          appendInline(item, lines[index].trim().replace(/^\d+\.\s+/, ''));
          list.append(item);
          index += 1;
        }
        section.append(list);
        continue;
      }
      if (/^\*\*.+\*\*$/.test(line) && line.includes('→')) {
        const callout = make('p', 'flow-callout');
        appendInline(callout, line);
        section.append(callout);
        index += 1;
        continue;
      }
      if (/^\*\*[^:]+:\*\*/.test(line)) { index += 1; continue; }

      const parts = [line];
      index += 1;
      while (index < lines.length) {
        const next = lines[index].trim();
        if (!next) { index += 1; break; }
        if (/^(#|\||-|\d+\.\s)/.test(next) || next === '---') break;
        parts.push(next);
        index += 1;
      }
      const paragraph = make('p');
      appendInline(paragraph, parts.join(' '));
      section.append(paragraph);
    }

    documentRoot.replaceChildren(content);
    tocRoot.replaceChildren(toc);
    documentRoot.setAttribute('aria-busy', 'false');
    installTocAccordions();
    installSectionObserver();
  }

  function installTocAccordions() {
    const groups = [...tocRoot.querySelectorAll('.toc-group')];
    groups.forEach((group) => {
      group.addEventListener('toggle', () => {
        if (!group.open) return;
        groups.forEach((other) => {
          if (other !== group) other.open = false;
        });
      });
    });
  }

  function installSectionObserver() {
    const links = [...tocRoot.querySelectorAll('a')];
    const groups = [...tocRoot.querySelectorAll('.toc-group')];
    const lookup = new Map(links.map((link) => [link.dataset.target, link]));
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (!visible) return;
      links.forEach((link) => link.classList.remove('active'));
      groups.forEach((group) => group.classList.remove('is-current'));
      const activeLink = lookup.get(visible.target.id);
      activeLink?.classList.add('active');
      activeLink?.closest('.toc-group')?.classList.add('is-current');
    }, { rootMargin: '-82px 0px -72% 0px', threshold: 0 });
    documentRoot.querySelectorAll('.doc-section').forEach((section) => observer.observe(section));
  }

  function applySearch() {
    const query = searchInput.value.trim().toLowerCase();
    const sections = [...documentRoot.querySelectorAll('.doc-section')];
    let visible = 0;
    sections.forEach((section) => {
      const match = !query || section.textContent.toLowerCase().includes(query);
      section.hidden = !match;
      if (match) visible += 1;
    });
    searchStatus.textContent = query ? `${visible} section${visible === 1 ? '' : 's'} found` : 'Full document';
    documentRoot.querySelector('.empty-search')?.remove();
    if (query && visible === 0) documentRoot.append(make('div', 'empty-search', `No blueprint section contains “${searchInput.value.trim()}”.`));
  }

  document.querySelectorAll('[data-reader-platform]').forEach((link) => {
    if (link.dataset.readerPlatform === platform) link.setAttribute('aria-current', 'page');
  });
  searchInput.addEventListener('input', applySearch);
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchInput.focus(); }
  });
  document.querySelector('#print-blueprint').addEventListener('click', () => window.print());

  const themeButton = document.querySelector('#reader-theme-toggle');
  const savedTheme = localStorage.getItem('forge-architecture-theme');
  if (savedTheme === 'light' || savedTheme === 'dark') document.documentElement.dataset.theme = savedTheme;
  themeButton.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('forge-architecture-theme', next);
  });

  const progress = document.querySelector('#reading-progress-bar');
  function updateProgress() {
    const available = document.documentElement.scrollHeight - window.innerHeight;
    progress.style.width = `${available > 0 ? Math.min(100, (window.scrollY / available) * 100) : 0}%`;
  }
  addEventListener('scroll', updateProgress, { passive: true });
  addEventListener('resize', updateProgress, { passive: true });

  fetch(source, { credentials: 'same-origin', headers: { Accept: 'text/markdown' } })
    .then((response) => { if (!response.ok) throw new Error(`Blueprint source returned ${response.status}`); return response.text(); })
    .then(renderMarkdown)
    .catch((error) => {
      documentRoot.setAttribute('aria-busy', 'false');
      documentRoot.replaceChildren(make('div', 'empty-search', `The blueprint could not be displayed. ${error.message}`));
    });
})();
