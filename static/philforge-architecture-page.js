(() => {
  'use strict';

  const atlasMarkup = `
    <div class="atlas-shell">
      <section class="control-deck" aria-label="Platform selection">
        <div class="platform-tabs" role="tablist" aria-label="Architecture platform">
          <button type="button" role="tab" aria-selected="true" aria-controls="platform-view" tabindex="0" data-platform="estate">
            <span class="tab-sigil estate"></span><span><strong>Forge Estate</strong><small>Shared control plane</small></span>
          </button>
          <button type="button" role="tab" aria-selected="false" aria-controls="platform-view" tabindex="-1" data-platform="crypto">
            <span class="tab-sigil crypto"></span><span><strong>CryptoForge</strong><small>Digital assets · 24/7</small></span>
          </button>
          <button type="button" role="tab" aria-selected="false" aria-controls="platform-view" tabindex="-1" data-platform="phil">
            <span class="tab-sigil phil"></span><span><strong>PhilForge</strong><small>Indian markets · IST</small></span>
          </button>
        </div>
        <div class="context-readout"><span>ACTIVE VIEW</span><strong id="active-view-name">FORGE ESTATE</strong></div>
      </section>

      <section id="platform-view" class="platform-view" role="tabpanel">
        <div class="section-head">
          <div><p class="eyebrow"><span>LIVE MAP</span> Central wiring</p><h2 id="map-title">Shared request-to-audit path</h2></div>
          <p id="map-subtitle">The same guarded pattern supports two different markets without mixing their accounts, clocks, brokers, or execution rules.</p>
        </div>
        <div class="flow-board">
          <div class="flow-rail" aria-hidden="true"><span></span></div>
          <ol class="flow-nodes" id="flow-nodes" aria-label="System flow stages"></ol>
          <div class="flow-foot">
            <div><span class="legend-pulse teal"></span> Request and command</div>
            <div><span class="legend-pulse violet"></span> Identity and risk gate</div>
            <div><span class="legend-pulse amber"></span> External system boundary</div>
            <div class="flow-rule" id="flow-rule">No browser action can bypass the API, risk checks, or audit write.</div>
          </div>
        </div>
        <div class="metric-strip" id="metric-strip" aria-label="Platform characteristics"></div>
        <div class="atlas-grid">
          <article class="panel span-7">
            <div class="panel-head"><div><span class="panel-index">A</span><h3>Input → processing → output</h3></div><span class="panel-tag">DATA CONTRACT</span></div>
            <div class="io-map" id="io-map"></div>
          </article>
          <article class="panel span-5">
            <div class="panel-head"><div><span class="panel-index">B</span><h3>Trust boundaries</h3></div><span class="panel-tag">ZERO SHORTCUTS</span></div>
            <div class="trust-map">
              <div class="trust-rings" aria-hidden="true"><span class="ring ring-1"></span><span class="ring ring-2"></span><span class="ring ring-3"></span><b>USER</b></div>
              <ol id="trust-list"></ol>
            </div>
          </article>
          <article class="panel span-7">
            <div class="panel-head"><div><span class="panel-index">C</span><h3>Runtime cadence</h3></div><span class="panel-tag">CLOCK-AWARE</span></div>
            <div class="cadence-chart" id="cadence-chart"></div>
          </article>
          <article class="panel span-5">
            <div class="panel-head"><div><span class="panel-index">D</span><h3>System ownership</h3></div><span class="panel-tag">ONE OWNER PER STATE</span></div>
            <div class="ownership" id="ownership"></div>
          </article>
        </div>
      </section>

      <section class="comparison" aria-labelledby="architecture-comparison-title">
        <div class="section-head"><div><p class="eyebrow"><span>COMPARE</span> Platform boundaries</p><h2 id="architecture-comparison-title">Same discipline, different market realities</h2></div></div>
        <div class="comparison-table" role="table" aria-label="CryptoForge and PhilForge comparison">
          <div class="comparison-row comparison-header" role="row"><div role="columnheader">Dimension</div><div role="columnheader"><i class="dot crypto"></i>CryptoForge</div><div role="columnheader"><i class="dot phil"></i>PhilForge</div></div>
          <div class="comparison-row" role="row"><div role="rowheader">Market</div><div role="cell">Crypto spot and derivatives</div><div role="cell">NSE equities and index options</div></div>
          <div class="comparison-row" role="row"><div role="rowheader">Operating clock</div><div role="cell">Continuous 24 × 7 runtime</div><div role="cell">IST sessions, expiries and holidays</div></div>
          <div class="comparison-row" role="row"><div role="rowheader">Primary integration</div><div role="cell">Binance; CoinDCX and Delta adapters</div><div role="cell">Dhan execution; Upstox history</div></div>
          <div class="comparison-row" role="row"><div role="rowheader">Execution ownership</div><div role="cell">Server engines plus signed buyer executor</div><div role="cell">User-scoped server engines</div></div>
          <div class="comparison-row" role="row"><div role="rowheader">Persistence</div><div role="cell">SQLite and JSON state</div><div role="cell">SQLite WAL with user boundaries</div></div>
          <div class="comparison-row" role="row"><div role="rowheader">Failure posture</div><div role="cell">Reconcile exchange truth before continuing</div><div role="cell">Withhold unsafe action or P&amp;L when evidence is incomplete</div></div>
        </div>
      </section>

      <section class="deployment" aria-labelledby="architecture-deployment-title">
        <div class="section-head"><div><p class="eyebrow"><span>SHIP</span> Controlled delivery</p><h2 id="architecture-deployment-title">Blue-green deployment without trading interruption</h2></div><p>Every release earns promotion. The inactive service is built, checked and made healthy before traffic moves.</p></div>
        <ol class="deploy-track">
          <li><b>01</b><span><strong>Build</strong><small>Static assets and application package</small></span></li>
          <li><b>02</b><span><strong>Validate</strong><small>Unit, browser and safety checks</small></span></li>
          <li><b>03</b><span><strong>Start idle</strong><small>Launch the inactive port</small></span></li>
          <li><b>04</b><span><strong>Health gate</strong><small>API and page verification</small></span></li>
          <li><b>05</b><span><strong>Promote</strong><small>Nginx switches only after green</small></span></li>
        </ol>
      </section>

      <section class="truth-section" aria-labelledby="architecture-truth-title">
        <div class="section-head"><div><p class="eyebrow"><span>NON-NEGOTIABLE</span> Architecture truths</p><h2 id="architecture-truth-title">Rules the product must never quietly break</h2></div></div>
        <div class="truth-grid">
          <article><span>01</span><h3>Identity before state</h3><p>Every private page, command and record resolves an authenticated owner first.</p></article>
          <article><span>02</span><h3>Risk before execution</h3><p>An order is checked for mode, quantity, limits and broker readiness before it leaves.</p></article>
          <article><span>03</span><h3>Market truth before display</h3><p>Charts and P&amp;L never invent missing prices to make a screen look complete.</p></article>
          <article><span>04</span><h3>Audit after every action</h3><p>Requests, decisions, broker responses and recovery events remain explainable.</p></article>
        </div>
      </section>

      <section class="document-dock" aria-labelledby="architecture-documents-title">
        <div><p class="eyebrow"><span>DOCUMENTS</span> Complete references</p><h2 id="architecture-documents-title">Independent platform blueprints</h2><p>Open either full blueprint here without leaving the PhilForge application.</p></div>
        <div class="document-cards">
          <a href="#architecture/cryptoforge" data-architecture-view="cryptoforge"><i class="dot crypto"></i><span><strong>CryptoForge Blueprint</strong><small>Complete 24/7 digital-asset architecture</small></span><b>→</b></a>
          <a href="#architecture/philforge" data-architecture-view="philforge"><i class="dot phil"></i><span><strong>PhilForge Blueprint</strong><small>Complete Indian-market architecture</small></span><b>→</b></a>
        </div>
      </section>
    </div>`;

  class PhilForgeArchitectureAtlas extends HTMLElement {
    connectedCallback() {
      if (this.shadowRoot) return;
      const version = encodeURIComponent(this.dataset.assetVersion || 'current');
      const root = this.attachShadow({ mode: 'open' });
      root.innerHTML = `
        <link rel="stylesheet" href="/static/architecture.css?v=${version}">
        <style>
          :host {
            --bg: transparent;
            --bg-grid: transparent;
            --surface: var(--panel, #0c1320);
            --surface-2: var(--panel-hi, #101a2a);
            --surface-3: rgba(122, 153, 190, .10);
            --line: var(--border, rgba(126,157,196,.18));
            --line-strong: var(--border-hi, rgba(126,157,196,.32));
            --dim: var(--muted, #5f6c80);
            --teal: #27d3b4;
            --teal-rgb: 39, 211, 180;
            --blue: #75adff;
            --blue-rgb: 117, 173, 255;
            --violet: #a78bfa;
            --amber: #f5b84b;
            --red: #fa7781;
            --radius: 16px;
            --mono: var(--font-mono, 'JetBrains Mono', monospace);
            --sans: var(--font-body, 'Outfit', sans-serif);
            display: block;
            min-width: 0;
          }
          .atlas-shell { width: 100%; }
          .control-deck { top: 8px; margin-top: 0; }
          .document-dock { margin-bottom: 0; }
          :host-context(html[data-theme="light"]) .control-deck,
          :host-context(html[data-theme="light"]) .panel,
          :host-context(html[data-theme="light"]) .comparison-row { background: rgba(249,251,253,.86); }
          :host-context(html[data-theme="light"]) .flow-board { background: linear-gradient(145deg, #f9fbfd, #edf3f8); }
          @media (max-width: 780px) { .atlas-shell { width: 100%; } }
        </style>
        ${atlasMarkup}`;
      root.querySelectorAll('[data-architecture-view]').forEach((link) => {
        link.addEventListener('click', (event) => {
          event.preventDefault();
          this.dispatchEvent(new CustomEvent('architecture-view-change', {
            bubbles: true,
            composed: true,
            detail: { view: link.dataset.architectureView },
          }));
        });
      });
      if (typeof window.initArchitectureAtlas === 'function') window.initArchitectureAtlas(root);
    }
  }

  if (!customElements.get('philforge-architecture-atlas')) {
    customElements.define('philforge-architecture-atlas', PhilForgeArchitectureAtlas);
  }
})();
