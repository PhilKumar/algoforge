(function () {
  const INSTALL_SELECTOR = '[data-install-app]';
  let deferredPrompt = null;
  let registrationReady = false;
  let installDialog = null;

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  }

  function isIosSafari() {
    const ua = window.navigator.userAgent || '';
    const isIOS = /iphone|ipad|ipod/i.test(ua);
    const isSafari = /safari/i.test(ua) && !/crios|fxios|edgios/i.test(ua);
    return isIOS && isSafari;
  }

  function isChromiumLike() {
    const ua = window.navigator.userAgent || '';
    return /chrome|chromium|edg/i.test(ua) && !/opr|opera|fxios|firefox/i.test(ua);
  }

  function hasServiceWorkerControl() {
    return !!(navigator.serviceWorker && navigator.serviceWorker.controller);
  }

  function buttons() {
    return Array.from(document.querySelectorAll(INSTALL_SELECTOR));
  }

  function installMode() {
    if (isStandalone()) return 'installed';
    if (deferredPrompt) return 'prompt';
    if (isIosSafari()) return 'ios';
    return 'manual';
  }

  function manualInstallMessage() {
    return 'Install is available only in supported browsers once PhilForge is considered installable. Use Chrome or Edge over HTTPS, refresh once, then use the browser menu and choose "Install App" or "Add to Home Screen".';
  }

  function installDialogMarkup() {
    return `
      <div class="pf-pwa-overlay" hidden>
        <div class="pf-pwa-sheet" role="dialog" aria-modal="true" aria-labelledby="pf-pwa-title">
          <div class="pf-pwa-sheet-top">
            <div class="pf-pwa-sheet-copy">
              <div class="pf-pwa-kicker">PhilForge App Install</div>
              <h3 id="pf-pwa-title">Install guidance</h3>
            </div>
            <button type="button" class="pf-pwa-close" aria-label="Close install guidance">×</button>
          </div>
          <p class="pf-pwa-message"></p>
          <div class="pf-pwa-points"></div>
          <div class="pf-pwa-actions">
            <button type="button" class="pf-pwa-btn pf-pwa-btn-ghost" data-pwa-action="secondary" hidden></button>
            <button type="button" class="pf-pwa-btn pf-pwa-btn-primary" data-pwa-action="primary">OK</button>
          </div>
        </div>
      </div>
    `;
  }

  function handleInstallDialogKeydown(event) {
    if (event.key === 'Escape') closeInstallDialog();
  }

  function ensureInstallDialog() {
    if (installDialog) return installDialog;
    const style = document.createElement('style');
    style.textContent = `
      .pf-pwa-overlay {
        position: fixed;
        inset: 0;
        z-index: 5000;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 20px;
        background: rgba(4, 8, 16, 0.68);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
      }
      .pf-pwa-sheet {
        width: min(460px, calc(100vw - 28px));
        border-radius: 26px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background:
          linear-gradient(165deg, rgba(17, 28, 44, 0.97), rgba(10, 17, 29, 0.95)),
          rgba(11, 19, 32, 0.92);
        color: #e5edf7;
        box-shadow: 0 30px 70px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255,255,255,0.06);
        padding: 22px;
      }
      html[data-theme="light"] .pf-pwa-sheet {
        border-color: rgba(15, 23, 42, 0.08);
        background:
          linear-gradient(165deg, rgba(255, 255, 255, 0.98), rgba(246, 250, 253, 0.95)),
          rgba(255,255,255,0.95);
        color: #102031;
        box-shadow: 0 22px 52px rgba(15, 23, 42, 0.12), inset 0 1px 0 rgba(255,255,255,0.9);
      }
      .pf-pwa-sheet-top {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 14px;
        margin-bottom: 14px;
      }
      .pf-pwa-kicker {
        font: 700 10px/1 Outfit, sans-serif;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: rgba(197, 208, 226, 0.65);
        margin-bottom: 8px;
      }
      html[data-theme="light"] .pf-pwa-kicker {
        color: rgba(71, 85, 105, 0.72);
      }
      .pf-pwa-sheet h3 {
        margin: 0;
        font: 700 1.24rem/1 Syne, sans-serif;
        letter-spacing: -0.03em;
      }
      .pf-pwa-close,
      .pf-pwa-btn {
        appearance: none;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.08);
        background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
        color: inherit;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.08), 0 10px 24px rgba(0,0,0,0.16);
        backdrop-filter: blur(18px) saturate(1.15);
        -webkit-backdrop-filter: blur(18px) saturate(1.15);
      }
      html[data-theme="light"] .pf-pwa-close,
      html[data-theme="light"] .pf-pwa-btn {
        border-color: rgba(15, 23, 42, 0.08);
        background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(243,247,250,0.92));
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.85), 0 12px 24px rgba(15,23,42,0.08);
      }
      .pf-pwa-close {
        width: 38px;
        height: 38px;
        cursor: pointer;
        font-size: 21px;
        line-height: 1;
      }
      .pf-pwa-message {
        margin: 0 0 14px;
        font: 500 13px/1.65 Outfit, sans-serif;
        color: rgba(220, 230, 244, 0.82);
      }
      html[data-theme="light"] .pf-pwa-message {
        color: rgba(30, 41, 59, 0.86);
      }
      .pf-pwa-points {
        display: grid;
        gap: 10px;
        margin-bottom: 18px;
      }
      .pf-pwa-point {
        padding: 12px 14px;
        border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.05);
        background: rgba(255,255,255,0.03);
        font: 500 12px/1.55 Outfit, sans-serif;
        color: rgba(220, 230, 244, 0.84);
      }
      html[data-theme="light"] .pf-pwa-point {
        border-color: rgba(15,23,42,0.05);
        background: rgba(15,23,42,0.025);
        color: rgba(30, 41, 59, 0.84);
      }
      .pf-pwa-actions {
        display: flex;
        justify-content: flex-end;
        gap: 10px;
      }
      .pf-pwa-btn {
        min-height: 42px;
        padding: 0 16px;
        cursor: pointer;
        font: 600 12px/1 Outfit, sans-serif;
      }
      .pf-pwa-btn-primary {
        background: linear-gradient(180deg, rgba(49, 212, 191, 0.20), rgba(79, 142, 247, 0.14));
        border-color: rgba(49, 212, 191, 0.28);
        color: #eafaf6;
      }
      html[data-theme="light"] .pf-pwa-btn-primary {
        background: linear-gradient(180deg, rgba(232, 247, 243, 0.96), rgba(234, 243, 253, 0.94));
        color: #102031;
      }
      @media (max-width: 767px) {
        .pf-pwa-overlay { align-items: flex-end; padding: 14px; }
        .pf-pwa-sheet {
          width: 100%;
          border-radius: 24px 24px 18px 18px;
          padding: 20px 18px calc(18px + env(safe-area-inset-bottom));
        }
        .pf-pwa-actions {
          flex-direction: column-reverse;
        }
        .pf-pwa-btn,
        .pf-pwa-close {
          width: 100%;
        }
      }
    `;
    if (!document.getElementById('pf-pwa-style')) {
      style.id = 'pf-pwa-style';
      document.head.appendChild(style);
    }
    const mount = document.createElement('div');
    mount.innerHTML = installDialogMarkup();
    installDialog = mount.firstElementChild;
    document.body.appendChild(installDialog);
    return installDialog;
  }

  function closeInstallDialog() {
    if (!installDialog) return;
    document.removeEventListener('keydown', handleInstallDialogKeydown);
    installDialog.remove();
    installDialog = null;
  }

  function showInstallDialog({ title, message, points = [], primaryLabel = 'OK', secondaryLabel = '', onPrimary = null, onSecondary = null }) {
    closeInstallDialog();
    const dialog = ensureInstallDialog();
    dialog.querySelector('#pf-pwa-title').textContent = title;
    dialog.querySelector('.pf-pwa-message').textContent = message;
    const pointsEl = dialog.querySelector('.pf-pwa-points');
    pointsEl.innerHTML = points.map((point) => `<div class="pf-pwa-point">${point}</div>`).join('');
    const closeBtn = dialog.querySelector('.pf-pwa-close');
    const primary = dialog.querySelector('[data-pwa-action="primary"]');
    const secondary = dialog.querySelector('[data-pwa-action="secondary"]');
    const close = () => closeInstallDialog();
    closeBtn.addEventListener('click', (event) => {
      event.preventDefault();
      close();
    });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) close();
    });
    primary.textContent = primaryLabel;
    primary.addEventListener('click', (event) => {
      event.preventDefault();
      close();
      if (typeof onPrimary === 'function') onPrimary();
    });
    if (secondaryLabel) {
      secondary.hidden = false;
      secondary.textContent = secondaryLabel;
      secondary.addEventListener('click', (event) => {
        event.preventDefault();
        close();
        if (typeof onSecondary === 'function') onSecondary();
      });
    } else {
      secondary.hidden = true;
    }
    document.addEventListener('keydown', handleInstallDialogKeydown);
  }

  function syncButtons() {
    const mode = installMode();
    buttons().forEach((btn) => {
      btn.hidden = mode === 'installed';
      btn.disabled = false;
      btn.dataset.installMode = mode;
      if (!btn.dataset.defaultLabel && !btn.classList.contains('icon-only') && btn.childElementCount === 0) {
        btn.dataset.defaultLabel = (btn.textContent || '').trim() || 'Install App';
      }
      if (!btn.classList.contains('icon-only') && btn.childElementCount === 0) {
        btn.textContent = mode === 'ios'
          ? 'Add to Home'
          : (mode === 'prompt' ? 'Install App' : 'Install Guide');
      }
      if (mode === 'prompt') {
        btn.title = 'Install App';
        btn.setAttribute('aria-label', 'Install App');
      } else if (mode === 'ios') {
        btn.title = 'Add to Home Screen';
        btn.setAttribute('aria-label', 'Add to Home Screen');
      } else if (mode === 'manual') {
        btn.title = registrationReady ? 'Install Guide' : 'Install App (Preparing offline support)';
        btn.setAttribute('aria-label', 'Install App');
      }
    });
  }

  async function openInstallPrompt() {
    if (isStandalone()) return;
    if (deferredPrompt) {
      deferredPrompt.prompt();
      try {
        await deferredPrompt.userChoice;
      } catch (e) {}
      deferredPrompt = null;
      syncButtons();
      return;
    }
    if (isIosSafari()) {
      showInstallDialog({
        title: 'Add PhilForge to Home Screen',
        message: 'iPhone does not show the same install prompt as desktop Chrome. Use Safari’s share menu instead.',
        points: [
          'Tap the Share button in Safari.',
          'Choose "Add to Home Screen".',
          'Open PhilForge from the new icon for the app-like full-screen view.',
        ],
      });
      return;
    }
    if (!hasServiceWorkerControl() && registrationReady) {
      showInstallDialog({
        title: 'Reload once to finish install setup',
        message: 'PhilForge has registered its app shell, but this tab is not yet controlled by the service worker. One reload is usually enough before Chrome exposes the install option.',
        points: [
          'Reload this page once.',
          'Then retry the install button or use the install icon in the address bar.',
          'If Chrome still does not show it, open the browser menu and choose "Install page as app".',
        ],
        primaryLabel: 'Reload Now',
        secondaryLabel: 'Close',
        onPrimary: () => window.location.reload(),
      });
      return;
    }
    const chromeSpecific = isChromiumLike();
    showInstallDialog({
      title: chromeSpecific ? 'Chrome has not exposed the install prompt yet' : 'Install guidance',
      message: chromeSpecific
        ? 'You are in a Chromium browser, but the browser has not surfaced the native install prompt for this page yet.'
        : manualInstallMessage(),
      points: chromeSpecific
        ? [
            'Look for the install icon in the address bar.',
            'Or open the browser menu and choose "Install page as app".',
            'If this is your first visit after the update, refresh once and try again.',
          ]
        : [
            'Use Chrome or Edge over HTTPS.',
            'Refresh once after the app shell is installed.',
            'Then use the browser menu and choose "Install App" or "Add to Home Screen".',
          ],
      primaryLabel: chromeSpecific ? 'Reload Now' : 'Got It',
      secondaryLabel: chromeSpecific ? 'Close' : '',
      onPrimary: chromeSpecific ? () => window.location.reload() : null,
    });
  }

  function bindInstallButtons() {
    buttons().forEach((btn) => {
      if (btn.dataset.installBound === '1') return;
      btn.dataset.installBound = '1';
      btn.addEventListener('click', openInstallPrompt);
    });
  }

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt = event;
    bindInstallButtons();
    syncButtons();
  });

  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    syncButtons();
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').then(() => {
        registrationReady = true;
        syncButtons();
      }).catch(() => {
        registrationReady = false;
        syncButtons();
      });
    });
  }

  window.PhilForgePWA = {
    openInstallPrompt,
    syncButtons,
    getInstallState() {
      return {
        mode: installMode(),
        hasPrompt: !!deferredPrompt,
        registrationReady,
        standalone: isStandalone(),
        iosSafari: isIosSafari(),
      };
    },
  };

  bindInstallButtons();
  syncButtons();
})();
