// =====================================================================
// wp-core.js — State global, utilidades, API helpers, constantes
// Carregado primeiro: não depende de nenhum outro módulo WP.
// =====================================================================

/* ---------- State global ---------- */
const state = {
  url:          localStorage.getItem('wp_url')    || 'https://api.workpulse.com.br',
  tenant:       '',          // derivado do login (e-mail/senha) ou informado no acesso técnico
  token:        '',          // sentinela de sessão ('__session__') OU token de acesso técnico (break-glass)
  breakGlass:   false,       // true quando o acesso é por tenant+token (suporte)
  authed:       false,
  role:         '',          // 'admin_master' | 'supervisor' | 'wp_admin'
  email:        '',
  displayName:  '',
  mustChangePw: false,
  charts:       {},
  people:       [],
  lastOverview: null,
  collabs:      [],
  vacation:     new Set(),   // user_ids em férias (rótulo) — preenchido por markVacation()
  stock:        new Set(),   // user_ids de máquinas em ESTOQUE (rótulo 📦) — idem
  lunchMin:     60,          // minutos de almoço descontados do AFK por dia (config Alertas)
};

/* ---------- Limite p/ considerar 'online' (espelha o backend) ---------- */
const WP_ONLINE_MINUTES = 30;

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

// Formato ÚNICO de duração longa (hh:mm) — mesma unidade na Jornada e no
// Consolidado, para as telas conversarem (antes: "7.7h" vs "07:41").
function fmtHM(s) {
  if (s == null || isNaN(s)) return '—';
  if (s < 60) return Math.round(s || 0) + 's';
  let h = Math.floor(s / 3600);
  let m = Math.round((s % 3600) / 60);
  if (m === 60) { h += 1; m = 0; }
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
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

/* ---------- Férias (rótulo de ausência justificada) — compartilhado ---------- */
// Atualiza state.vacation a partir de qualquer lista de usuários que tenha
// `on_vacation`. Guarda o user_id cru E o "limpo" p/ casar com qualquer tela.
function markVacation(users) {
  if (!Array.isArray(users)) return;
  for (const u of users) {
    if (!u || !u.user_id) continue;
    if (u.on_vacation) { state.vacation.add(u.user_id); state.vacation.add(cleanUserId(u.user_id)); }
    else { state.vacation.delete(u.user_id); state.vacation.delete(cleanUserId(u.user_id)); }
    if (u.in_stock) { state.stock.add(u.user_id); state.stock.add(cleanUserId(u.user_id)); }
    else { state.stock.delete(u.user_id); state.stock.delete(cleanUserId(u.user_id)); }
  }
}
function isVacation(uid) { return !!uid && state.vacation.has(uid); }
function isStock(uid)    { return !!uid && state.stock.has(uid); }
// Quem pode marcar/desmarcar Estoque (backend valida de novo no PATCH).
function canMarkStock()  { return ['admin_master', 'suporte', 'wp_admin'].includes(state.role); }
// Badge HTML (para listas/tabelas). Vazio quando não está de férias nem em estoque.
function vacBadge(uid) {
  if (isStock(uid))
    return ' <span title="Máquina de estoque (sem usuário) — fora da Visão Geral, Tempo Ativo e alertas" style="display:inline-block;font-size:10px;font-weight:600;color:#92400e;background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:1px 6px;vertical-align:middle">📦 Estoque</span>';
  return isVacation(uid)
    ? ' <span title="Em férias" style="display:inline-block;font-size:10px;font-weight:600;color:#0e7490;background:#cffafe;border:1px solid #a5f3fc;border-radius:6px;padding:1px 6px;vertical-align:middle">🏖 Férias</span>'
    : '';
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
  markVacation(users);
  const sorted = [...users].sort((a, b) => displayName(a).localeCompare(displayName(b)));
  sorted.forEach(u => {
    const opt = document.createElement('option');
    opt.value       = cleanUserId(u.user_id);
    opt.textContent = displayName(u) + (u.in_stock ? ' 📦 (Estoque)' : (u.on_vacation ? ' 🏖 (Férias)' : ''));
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
function _authHeaders(extra) {
  // Sessão por cookie é enviada automaticamente (credentials:'include').
  // Só mandamos X-Admin-Token no acesso técnico (break-glass).
  const h = extra ? { ...extra } : {};
  if (state.breakGlass && state.token && state.token !== '__session__') {
    h['X-Admin-Token'] = state.token;
  }
  return h;
}

function _onAuthFail() {
  if (typeof _handleAuthFail === 'function') _handleAuthFail();
}

/* Atalho "ver pessoa em X": a tela Pessoas seta _deepLinkUser e troca de aba;
   o loader da aba-destino consome via _applyDeepLinkUser() (select) ou lê o nome. */
let _deepLinkUser = null;   // { uid, name }
function _applyDeepLinkUser(selectId) {
  if (!_deepLinkUser) return null;
  const u = _deepLinkUser; _deepLinkUser = null;
  const sel = document.getElementById(selectId);
  if (sel) sel.value = u.uid;
  return u;
}

/* Pula para outra aba mantendo o colaborador selecionado no select de origem.
   Ex.: da Internet → Aplicativos com a mesma pessoa filtrada (e vice-versa). */
function jumpUserTo(fromSelectId, page) {
  const sel = document.getElementById(fromSelectId);
  const uid = sel ? sel.value.trim() : '';
  if (!uid) { if (typeof toast === 'function') toast('Selecione um colaborador primeiro.'); return; }
  const name = (sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) ? sel.options[sel.selectedIndex].text : '';
  if (typeof goToUserPage === 'function') goToUserPage(page, uid, name);
}

async function api(path, signal) {
  const timeout = new AbortController();
  const tid     = setTimeout(() => timeout.abort(), 90000);
  const combined = signal
    ? (AbortSignal.any ? AbortSignal.any([signal, timeout.signal]) : timeout.signal)
    : timeout.signal;
  _showLoader();
  try {
    const r = await fetch(state.url + path, {
      headers:     _authHeaders(),
      credentials: 'include',
      signal:      combined,
    });
    clearTimeout(tid);
    if (r.status === 401) { _onAuthFail(); throw new Error('401: sessão expirada'); }
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
      headers:     _authHeaders({ 'Content-Type': 'application/json' }),
      credentials: 'include',
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { _onAuthFail(); throw new Error('Sessão expirada'); }
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
