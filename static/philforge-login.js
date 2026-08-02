const PIN_LENGTH = 6;
const PASSWORD_MODE = 'password';
const PIN_MODE = 'pin';
let pin = '';
let locked = false;
let loginMode = PASSWORD_MODE;

const dots = document.querySelectorAll('.pin-dot');
const status = document.getElementById('unlock-status');
const card = document.getElementById('unlock-card');
const usernameInput = document.getElementById('username-input');
const passwordInput = document.getElementById('password-input');
const passwordField = document.getElementById('password-field');
const passwordToggle = document.getElementById('password-toggle');
const pinSection = document.getElementById('pin-section');
const modeSwitchBtn = document.getElementById('mode-switch-btn');
const unlockBtn = document.getElementById('unlock-btn');
const keypad = document.getElementById('keypad');

function baseStatusMessage() {
  return 'Enter username & password';
}

function updateDots() {
  dots.forEach((dot, i) => {
    dot.classList.remove('filled', 'error', 'success');
    if (i < pin.length) dot.classList.add('filled');
  });
}

function resetSecrets(clearPassword = true) {
  pin = '';
  updateDots();
  if (clearPassword) passwordInput.value = '';
}

function setIdleStatus() {
  status.textContent = baseStatusMessage();
  status.className = 'unlock-status';
  unlockBtn.disabled = false;
}

function setMode(mode, focus = true) {
  loginMode = PASSWORD_MODE;
  passwordField.classList.remove('hidden');
  pinSection.classList.add('hidden');
  modeSwitchBtn.classList.add('hidden');
  modeSwitchBtn.setAttribute('aria-hidden', 'true');
  unlockBtn.textContent = 'Unlock';
  resetSecrets(true);
  locked = false;
  setIdleStatus();
  if (!focus) return;
  if (!usernameInput.value.trim()) usernameInput.focus();
  else passwordInput.focus();
}

function showValidationError(msg, focusEl = null) {
  status.textContent = msg;
  status.className = 'unlock-status error';
  card.classList.add('shake');
  unlockBtn.disabled = false;
  setTimeout(() => {
    card.classList.remove('shake');
    if (focusEl) focusEl.focus();
  }, 400);
}

function setError(msg) {
  status.textContent = msg;
  status.className = 'unlock-status error';
  if (loginMode === PIN_MODE) {
    dots.forEach(d => {
      d.classList.remove('filled');
      d.classList.add('error');
    });
  }
  card.classList.add('shake');
  setTimeout(() => {
    card.classList.remove('shake');
    resetSecrets(true);
    locked = false;
    setIdleStatus();
    if (loginMode === PASSWORD_MODE) passwordInput.focus();
  }, 800);
}

function setSuccess() {
  status.textContent = 'Unlocked! Redirecting...';
  status.className = 'unlock-status success';
  if (loginMode === PIN_MODE) {
    dots.forEach(d => {
      d.classList.remove('filled');
      d.classList.add('success');
    });
  }
  card.classList.add('unlock-pulse');
}

async function tryUnlock() {
  if (locked) return;
  const username = usernameInput.value.trim();
  const secret = loginMode === PIN_MODE ? pin : passwordInput.value;

  if (!secret) {
    showValidationError(loginMode === PIN_MODE ? 'Enter your PIN' : 'Enter your password', passwordInput);
    return;
  }

  locked = true;
  status.textContent = 'Verifying...';
  status.className = 'unlock-status';
  unlockBtn.disabled = true;

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(username ? { username, password: secret } : { password: secret, pin: secret })
    });
    if (res.ok) {
      if ('caches' in window) {
        const keys = await caches.keys().catch(() => []);
        await Promise.all(keys.filter(key => key.startsWith('philforge-shell-')).map(key => caches.delete(key)));
      }
      setSuccess();
      // The terminal is at /app; "/" is now the public landing page. Sending a
      // freshly logged-in user to "/" would show them marketing copy.
      setTimeout(() => { window.location.href = '/app'; }, 400);
    } else {
      const data = await res.json().catch(() => ({}));
      setError(data.detail || 'Wrong credentials. Try again.');
    }
  } catch (e) {
    setError('Connection error.');
  }
}

function addDigit(d) {
  if (loginMode !== PIN_MODE) return;
  if (locked || pin.length >= PIN_LENGTH) return;
  pin += d;
  updateDots();
  if (pin.length === PIN_LENGTH) {
    setTimeout(tryUnlock, 150);
  }
}

function removeDigit() {
  if (loginMode !== PIN_MODE) return;
  if (locked || pin.length === 0) return;
  pin = pin.slice(0, -1);
  updateDots();
}

function clearAll() {
  if (loginMode !== PIN_MODE || locked) return;
  pin = '';
  updateDots();
}

usernameInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    passwordInput.focus();
  }
});

passwordInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    tryUnlock();
  }
});

passwordToggle.addEventListener('click', () => {
  const nextType = passwordInput.type === 'password' ? 'text' : 'password';
  passwordInput.type = nextType;
  passwordToggle.textContent = nextType === 'password' ? 'Show' : 'Hide';
  passwordToggle.setAttribute('aria-label', nextType === 'password' ? 'Show password' : 'Hide password');
});

modeSwitchBtn.addEventListener('click', () => {
  setMode(loginMode === PIN_MODE ? PASSWORD_MODE : PIN_MODE);
});

keypad.addEventListener('click', (e) => {
  const btn = e.target.closest('.key');
  if (!btn) return;
  const val = btn.dataset.val;
  if (val === 'clear') clearAll();
  else if (val === 'back') removeDigit();
  else addDigit(val);
});

document.addEventListener('keydown', (e) => {
  if (loginMode !== PIN_MODE) return;
  if (document.activeElement === usernameInput || document.activeElement === passwordInput) return;
  if (e.key >= '0' && e.key <= '9') addDigit(e.key);
  else if (e.key === 'Backspace') removeDigit();
  else if (e.key === 'Enter' && pin.length === PIN_LENGTH) tryUnlock();
  else if (e.key === 'Escape') clearAll();
});

unlockBtn.addEventListener('click', tryUnlock);
setMode(PASSWORD_MODE, false);
