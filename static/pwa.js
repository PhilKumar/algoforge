(function () {
  const INSTALL_SELECTOR = '[data-install-app]';
  let deferredPrompt = null;

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

  function syncButtons() {
    const installed = isStandalone();
    const showPrompt = !!deferredPrompt;
    const showIosHint = !installed && !showPrompt && isIosSafari();
    buttons().forEach((btn) => {
      btn.hidden = !(showPrompt || showIosHint);
      btn.dataset.installMode = showPrompt ? 'prompt' : (showIosHint ? 'ios' : '');
      if (showPrompt) {
        btn.title = 'Install App';
        btn.setAttribute('aria-label', 'Install App');
      } else if (showIosHint) {
        btn.title = 'Add to Home Screen';
        btn.setAttribute('aria-label', 'Add to Home Screen');
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
    }
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
      navigator.serviceWorker.register('/sw.js').catch(() => undefined);
    });
  }

  window.PhilForgePWA = {
    openInstallPrompt,
    syncButtons,
  };

  bindInstallButtons();
  syncButtons();
})();
