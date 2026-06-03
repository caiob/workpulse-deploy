// =====================================================================
// wp-core.js — State global, utilidades, API helpers, constantes
// Carregado primeiro: não depende de nenhum outro módulo WP.
// =====================================================================

/* ---------- State global ---------- */
const state = {
  url:          localStorage.getItem('wp_url')    || 'https://api.workpulse.com.br',
  tenant:       localStorage.getItem('wp_tenant') || '',
  token:        localStorage.getItem('wp_token')  || '',
  charts:       {},
  people:       [],
  lastOverview: null,
  collabs:      [],
};

/* ---------- Constantes de categoria ---------- */
const CAT_PILLS = {
  productive:    '<span class="pill productive">Produtivo</span>',
  unproductive:  '<span class="pill unproductive">Não-produtivo</span>',
  neutral:       '<span class="pill neutral">Neutro</span>',
  uncategorized: '<span class="pill uncategorized">Não categorizado</span>',
};
const CAT_COLORS = {
  productive:    'rgba(74,222,128,.82)',
  unproductive:  'rgba(248,113,113,.82)',
  neutral:       'rgba(148,163,184,.65)',
  uncategorized: 'rgba(245,158,11,.65)',
};

/* ---------- Cores de chart dinâmicas por tema ---------- */
function chartTheme() {
  const light = document.body.classList.contains('theme-light');
  return {
    label:  light ? '#4a5578' : '#8c97c4',
    grid:   light ? 'rgba(0,0,0,.07)' : 'rgba(36,48,112,.4)',
    legend: light ? '#1a2248' : '#cbd5ff',
    border: light ? '#ffffff' : '#131c3e',
  };
}

/* ---------- Status de conexão (LED + texto no dropdown) ---------- */
function _setConnectionStatus(text, isError) {
  const statusEl = document.getElementById('apiStatus');
  const ledEl    = document.getElementById('statusLed');
  if (statusEl) {
    statusEl.textContent = text;
    statusEl.className   = 'menu-info-value' + (isError ? ' error' : '');
  }
  if (ledEl) {
    if (isError) {
      ledEl.style.background  = 'var(--danger)';
      ledEl.style.boxShadow   = '0 0 8px var(--danger)';
    } else {
      ledEl.style.background  = '';
      ledEl.style.boxShadow   = '';
    }
  }
}

/* ---------- Toast ---------- */
function toast(msg, ms = 2800) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hide');
  setTimeout(() => el.classList.add('hide'), ms);
}

/* ---------- Formatadores ---------- */
function fmtH(seconds) {
  return ((seconds || 0) / 3600).toFixed(1);
}

function fmtSec(s) {
  if (!s || s < 60) return Math.round(s || 0) + 's';
  const h = s / 3600;
  return h < 1 ? (s / 60).toFixed(0) + 'min' : h.toFixed(1) + 'h';
}

function relTime(iso) {
  if (!iso) return '—';
  const d   = new Date(iso);
  const sec = (Date.now() - d) / 1000;
  if (sec < 60)    return 'agora';
  if (sec < 3600)  return Math.floor(sec / 60) + 'min';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h';
  return Math.floor(sec / 86400) + 'd';
}

function ptDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
}

function avatar(name, lg = false) {
  const initials = (name || '?')
    .replace(/[^A-Za-z0-9 ]/g, ' ')
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map(s => s[0] || '')
    .join('') || '?';
  return `<span class="avatar${lg ? ' lg' : ''}">${initials.slice(0, 2)}</span>`;
}

/* Strips the legacy "TENANTID:HOSTNAME" prefix — shows only hostname. */
function cleanUserId(uid) {
  if (!uid) return '—';
  const i = uid.indexOf(':');
  return i >= 0 ? uid.slice(i + 1) : uid;
}

function displayName(u) {
  return (u && (u.display_name || cleanUserId(u.user_id))) || '—';
}

/* ---------- User dropdown helper (compartilhado entre abas) ---------- */
async function _populateUserDropdown(selectId) {
  const sel = document.getElementById(selectId);
  if (!sel || sel.options.length > 1) return; // já populado
  let users = state.people;
  if (!users.length) {
    try {
      const data = await api(`/v1/users?tenant=${encodeURIComponent(state.tenant)}`);
      users = data.users || [];
    } catch (_) { return; }
  }
  const sorted = [...users].sort((a, b) => displayName(a).localeCompare(displayName(b)));
  sorted.forEach(u => {
    const opt = document.createElement('option');
    opt.value       = cleanUserId(u.user_id);
    opt.textContent = displayName(u);
    sel.appendChild(opt);
  });
}

/* ---------- Loading bar global ---------- */
let _loaderCount = 0;
let _loaderTimer = null;
function _showLoader() {
  if (++_loaderCount === 1) {
    // Só revela o overlay se a carga passar de 300ms (evita piscar em chamadas rápidas).
    _loaderTimer = setTimeout(() => {
      document.getElementById('globalLoader').classList.remove('hide');
    }, 300);
  }
}
function _hideLoader() {
  if (--_loaderCount <= 0) {
    _loaderCount = 0;
    if (_loaderTimer) { clearTimeout(_loaderTimer); _loaderTimer = null; }
    document.getElementById('globalLoader').classList.add('hide');
  }
}

/* ---------- Abort controller (cancelar requests ao trocar de aba) ---------- */
let _tabAbort = null;
function _cancelPending() {
  if (_tabAbort) _tabAbort.abort();
  _tabAbort = new AbortController();
  return _tabAbort.signal;
}

/* ---------- API (GET com token + timeout 90s) ---------- */
async function api(path, signal) {
  const timeout = new AbortController();
  const tid     = setTimeout(() => timeout.abort(), 90000);
  const combined = signal
    ? (AbortSignal.any ? AbortSignal.any([signal, timeout.signal]) : timeout.signal)
    : timeout.signal;
  _showLoader();
  try {
    const r = await fetch(state.url + path, {
      headers: { 'X-Admin-Token': state.token },
      signal:  combined,
    });
    clearTimeout(tid);
    if (!r.ok) {
      const t = await r.text().catch(() => r.statusText);
      throw new Error(`${r.status}: ${t}`);
    }
    return r.json();
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Tempo esgotado ou aba trocada');
    throw e;
  } finally {
    _hideLoader();
  }
}

/* ---------- API helper (POST/PATCH/DELETE com body JSON) ---------- */
async function apiFetch(method, path, body) {
  _showLoader();
  try {
    const r = await fetch(state.url + path, {
      method,
      headers: { 'X-Admin-Token': state.token, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) { const t = await r.text(); throw new Error(t); }
    return r.json().catch(() => ({}));
  } finally {
    _hideLoader();
  }
}

/* ---------- Formatador data+hora pt-BR ---------- */
function ptDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('pt-BR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/* ---------- HTML escape ---------- */
function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
