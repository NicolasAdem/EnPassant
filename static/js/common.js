// EnPassant shared utilities

const EP = {
  /* ------------- API ------------- */
  async api(method, path, body) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    if (!res.ok) {
      const msg = (data && data.detail) || `Error ${res.status}`;
      throw new Error(msg);
    }
    return data;
  },

  /* ------------- WebSocket ------------- */
  ws: null,
  wsReconnectTimer: null,

  connectWs(tid, onMessage) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/${tid}`;
    let active = true;

    const open = () => {
      this.ws = new WebSocket(url);
      this.ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          onMessage(msg);
        } catch (e) {
          console.error('Bad ws message', e);
        }
      };
      this.ws.onclose = () => {
        if (!active) return;
        // Auto-reconnect after a short delay
        clearTimeout(this.wsReconnectTimer);
        this.wsReconnectTimer = setTimeout(open, 1500);
      };
      this.ws.onerror = () => { this.ws.close(); };
    };
    open();
    return () => { active = false; this.ws && this.ws.close(); };
  },

  /* ------------- Toasts ------------- */
  toast(message, kind = 'info') {
    let stack = document.querySelector('.toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'toast-stack';
      document.body.appendChild(stack);
    }
    const t = document.createElement('div');
    t.className = `toast ${kind === 'error' ? 'error' : ''}`;
    t.textContent = message;
    stack.appendChild(t);
    setTimeout(() => {
      t.style.transition = 'opacity 0.3s, transform 0.3s';
      t.style.opacity = '0';
      t.style.transform = 'translateX(100%)';
      setTimeout(() => t.remove(), 300);
    }, 3200);
  },

  /* ------------- En Passant loader ------------- */
  loaderHtml(label = 'Loading') {
    return `
      <div class="en-passant-loader" aria-label="Loading">
        <div class="ep-board">
          <div class="ep-square"></div>
          <div class="ep-square dark"></div>
          <div class="ep-square"></div>
          <div class="ep-square dark"></div>
          <div class="ep-square dark"></div>
          <div class="ep-square"></div>
          <div class="ep-square dark"></div>
          <div class="ep-square"></div>
        </div>
        <div class="ep-pawn black" aria-hidden="true">♟</div>
        <div class="ep-pawn white" aria-hidden="true">♙</div>
      </div>
      <div class="ep-loader-label">${label}</div>
    `;
  },

  /* ------------- Helpers ------------- */
  escape(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  },

  formatScore(s) {
    // Drop trailing ".0", keep ".5"
    return Number(s) % 1 === 0 ? String(Number(s)) : Number(s).toFixed(1);
  },

  /* ------------- Theme (task #9) -------------
     The inline script in base.html applies the saved theme during
     <head> parsing to prevent a flash of the wrong palette. These
     helpers are for the PICKER on the host page — they keep the
     attribute, localStorage, meta theme-color, and the picker's
     own .is-checked classes in sync, and they fire a 'ep:theme'
     event so any open page on the same machine can react.

     Theme names are validated here too — an unknown name silently
     resolves to 'forest' rather than poisoning localStorage with a
     value the inline script wouldn't honor on next load. */
  THEMES: ['forest', 'royal', 'crimson', 'ivory'],
  THEME_BG: {
    forest: '#07100c', royal: '#060914',
    crimson: '#0a0606', ivory: '#f5f1e8',
  },
  getTheme() {
    try {
      const t = localStorage.getItem('ep_theme');
      return this.THEMES.includes(t) ? t : 'forest';
    } catch (e) { return 'forest'; }
  },
  applyTheme(name) {
    if (!this.THEMES.includes(name)) name = 'forest';
    // 'forest' is the :root default; removing the attribute is cleaner
    // than setting it (avoids a redundant selector match).
    if (name === 'forest') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', name);
    }
    try { localStorage.setItem('ep_theme', name); } catch (e) { /* private mode */ }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', this.THEME_BG[name]);
    // Notify any in-page listeners (the picker uses this to flip its
    // .is-checked class without re-querying the radios).
    window.dispatchEvent(new CustomEvent('ep:theme', { detail: { name } }));
  },
};

window.EP = EP;

// Task #9: cross-tab theme sync. The inline <head> script in base.html
// applies the saved theme on first paint, but if a page is ALREADY open
// when another tab changes the theme, it needs to re-theme without a
// reload. The storage event only fires in OTHER tabs (never the writer),
// so this is purely for the inheritor. Picker UIs handle their own
// reflection via the 'ep:theme' event.
window.addEventListener('storage', function(e) {
  if (e.key === 'ep_theme') {
    EP.applyTheme(EP.getTheme());
  }
});