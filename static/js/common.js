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
};

window.EP = EP;
