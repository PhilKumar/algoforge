(function () {
  const INSTALL_SELECTOR = '[data-install-app]';
  let deferredPrompt = null;
  let registrationReady = false;

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  }

  function isIosSafari() {
    const ua = window.navigator.userAgent || '';
    const isIOS = /iphone|ipad|ipod/i.test(ua);
    const isSafari = /safari/i.test(ua) && !/crios|fxios|edgios/i.test(ua);
    return isIOS && isSafari;
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

  function syncButtons() {
    const mode = installMode();
    buttons().forEach((btn) => {
      btn.hidden = mode === 'installed';
      btn.disabled = false;
      btn.dataset.installMode = mode;
      if (mode === 'prompt') {
        btn.title = 'Install App';
        btn.setAttribute('aria-label', 'Install App');
      } else if (mode === 'ios') {
        btn.title = 'Add to Home Screen';
        btn.setAttribute('aria-label', 'Add to Home Screen');
      } else if (mode === 'manual') {
        btn.title = registrationReady ? 'Install App (Open browser menu)' : 'Install App (Preparing offline support)';
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
      window.alert('To install PhilForge on iPhone: tap Share, then "Add to Home Screen".');
      return;
    }
    window.alert(manualInstallMessage());
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
