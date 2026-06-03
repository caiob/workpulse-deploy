// =====================================================================
// wp-auth.js — Autenticação, sessão, tema e menu de conta
// =====================================================================

/* ══════════════════════════════════════════════════════════════════
   TEMA
   ══════════════════════════════════════════════════════════════════ */

function applyTheme(theme) {
  document.body.classList.toggle('theme-light', theme === 'light');

  // Atualiza botão antigo em Configurações (compatibilidade)
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = theme === 'light' ? '🌙 Tema Escuro' : '☀️ Tema Claro';

  // Sincroniza toggle do menu de conta
  _acctSyncTheme();

  // Reconstrói charts com cores corretas
  Object.keys(state.charts).forEach(k => {
    if (state.charts[k] && typeof state.charts[k].destroy === 'function') {
      state.charts[k].destroy();
    }
    delete state.charts[k];
  });

  // Recarrega aba ativa
  if (state.token) {
    const activeBtn  = document.querySelector('nav.tabs > button.active');
    const activeItem = document.querySelector('.nav-dropdown-menu .active-item');
    const current    = activeItem || activeBtn;
    if (current?.dataset?.tab) switchTab(current.dataset.tab);
  }
}

function toggleTheme() {
  const isLight = document.body.classList.contains('theme-light');
  const next    = isLight ? 'dark' : 'light';
  localStorage.setItem('wp_theme', next);
  applyTheme(next);
}

/* ══════════════════════════════════════════════════════════════════
   MENU DE CONTA
   ══════════════════════════════════════════════════════════════════ */

/** Gradientes disponíveis para o avatar (escolhido pelo hash do tenant). */
const _ACCT_GRADIENTS = [
  ['#6c8cff', '#a78bfa'],
  ['#4ade80', '#22d3ee'],
  ['#f472b6', '#fb923c'],
  ['#f87171', '#a78bfa'],
  ['#22d3ee', '#6c8cff'],
  ['#fbbf24', '#4ade80'],
];

function _tenantGradient(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return _ACCT_GRADIENTS[h % _ACCT_GRADIENTS.length];
}

function _tenantInitials(name) {
  const parts = (name || '').trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return (name || 'WP').slice(0, 2).toUpperCase();
}

/** Sincroniza o toggle de tema do menu com o estado atual do body. */
function _acctSyncTheme() {
  const isLight = document.body.classList.contains('theme-light');
  const chk = document.getElementById('acctThemeCheck');
  const lbl = document.getElementById('acctThemeLbl');
  if (chk) chk.checked = isLight;
  if (lbl) lbl.textContent = isLight ? 'Claro' : 'Escuro';
}

/** Atualiza avatar, nome e ID no trigger e no cabeçalho do dropdown. */
function _acctSetTenant(tenantId, tenantName) {
  const name = tenantName || tenantId || 'WorkPulse';
  const initials = _tenantInitials(name);
  const [c1, c2] = _tenantGradient(tenantId || '');

  // Avatar no trigger
  const avatar = document.getElementById('acctAvatar');
  if (avatar) {
    avatar.textContent = initials;
    avatar.style.background = `linear-gradient(135deg, ${c1}, ${c2})`;
  }

  // Nome no trigger (truncado)
  const trigName = document.getElementById('acctTriggerName');
  if (trigName) trigName.textContent = name;

  // Cabeçalho do dropdown
  const nameEl = document.getElementById('acctTenantName');
  const idEl   = document.getElementById('acctTenantId');
  if (nameEl) nameEl.textContent = name;
  if (idEl)   idEl.textContent   = tenantId || '—';
}

/** Carrega informações de licença do servidor e popula o menu. */
async function _loadLicenseInfo() {
  if (!state.token || !state.tenant) return;
  try {
    const info = await api('/v1/license/info?tenant=' + encodeURIComponent(state.tenant));

    // Atualiza nome do tenant (agora com nome real)
    _acctSetTenant(info.tenant_id, info.tenant_name);

    // Plan badge
    const planEl = document.getElementById('acctPlan');
    if (planEl) {
      const planMap = { starter: 'Starter', business: 'Business', enterprise: 'Enterprise' };
      planEl.textContent = planMap[info.plan] || info.plan || '—';
      planEl.className   = 'acct-plan plan-' + (info.plan || 'starter');
    }

    // Seats
    const seatsEl = document.getElementById('acctSeats');
    if (seatsEl) {
      seatsEl.textContent = info.seats > 0
        ? `${info.used_seats} / ${info.seats}`
        : (info.used_seats > 0 ? info.used_seats : '—');
    }
  } catch (_) {
    // Falha silenciosa — dados do tenant ainda aparecem via _acctSetTenant
  }
}

/** Abre / fecha o menu de conta. */
function _toggleAccountMenu(e) {
  if (e) e.stopPropagation();
  const wrap = document.getElementById('acctWrap');
  if (!wrap) return;
  const isOpen = wrap.classList.contains('open');
  // Fecha nav-dropdowns antes
  document.querySelectorAll('.nav-dropdown.open').forEach(d => d.classList.remove('open'));
  wrap.classList.toggle('open', !isOpen);
}

/** Fecha o menu de conta. */
function _closeAccountMenu() {
  document.getElementById('acctWrap')?.classList.remove('open');
}

/** Inicializa wiring do menu de conta (click fora fecha, ESC fecha). */
function _acctInitMenu() {
  const trigger = document.getElementById('acctTrigger');
  const wrap    = document.getElementById('acctWrap');
  if (!trigger || !wrap) return;

  trigger.addEventListener('click', _toggleAccountMenu);

  // Click dentro do dropdown não propaga (não fecha)
  document.getElementById('acctDropdown')?.addEventListener('click', e => e.stopPropagation());

  // Click fora fecha
  document.addEventListener('click', () => _closeAccountMenu());

  // ESC fecha
  document.addEventListener('keydown', e => { if (e.key === 'Escape') _closeAccountMenu(); });
}

/* ══════════════════════════════════════════════════════════════════
   CONTROLE DE SESSÃO — expira por inatividade
   ══════════════════════════════════════════════════════════════════ */

// Sessão expira após 30 min sem atividade (clique/tecla/scroll/foco renovam).
const WP_SESSION_IDLE_MS = 30 * 60 * 1000;
let _wpIdleTimer = null;
let _wpLastWrite = 0;
let _wpWatchInit = false;

/** Renova o carimbo de atividade no localStorage (compartilhado entre abas).
 *  Throttle de 20s; grava na hora quando force=true (ex.: no login). */
function _wpTouchSession(force) {
  const now = Date.now();
  if (force || now - _wpLastWrite > 20000) {
    localStorage.setItem('wp_last_active', String(now));
    _wpLastWrite = now;
  }
}

/** True se a sessão estourou o tempo de inatividade. */
function _wpSessionExpired() {
  const la = parseInt(localStorage.getItem('wp_last_active') || '0', 10);
  return !la || (Date.now() - la) > WP_SESSION_IDLE_MS;
}

/** Encerra a sessão por inatividade: limpa o token e reabre o login. */
function _wpExpireSession() {
  localStorage.removeItem('wp_token');
  localStorage.removeItem('wp_last_active');
  state.token = '';
  if (_wpIdleTimer) { clearInterval(_wpIdleTimer); _wpIdleTimer = null; }
  const tk = document.getElementById('loginToken');
  if (tk) tk.value = '';
  document.getElementById('login').classList.remove('hide');
  _showLoginError('Sessão expirada por inatividade. Faça login novamente.');
}

/** Monitora atividade e checa inatividade a cada 30s. */
function _wpStartIdleWatch() {
  if (!_wpWatchInit) {
    ['mousedown', 'keydown', 'scroll', 'touchstart', 'visibilitychange'].forEach(ev =>
      window.addEventListener(ev, () => { if (state.token) _wpTouchSession(false); }, { passive: true })
    );
    _wpWatchInit = true;
  }
  if (_wpIdleTimer) clearInterval(_wpIdleTimer);
  _wpIdleTimer = setInterval(() => {
    if (state.token && _wpSessionExpired()) _wpExpireSession();
  }, 30000);
}

/* ══════════════════════════════════════════════════════════════════
   LOGIN
   ══════════════════════════════════════════════════════════════════ */

function _showLoginError(msg) {
  const el = document.getElementById('loginError');
  if (el) { el.textContent = msg; el.style.display = 'block'; }
}

function _clearLoginError() {
  const el = document.getElementById('loginError');
  if (el) el.style.display = 'none';
}

async function doLogin() {
  _clearLoginError();

  const url    = document.getElementById('loginUrl').value.trim().replace(/\/$/, '');
  const tenant = document.getElementById('loginTenant').value.trim();
  const token  = document.getElementById('loginToken').value.trim();

  if (!url || !tenant || !token) {
    _showLoginError('Preencha todos os campos para continuar.');
    return;
  }

  const btn          = document.querySelector('#login button.primary');
  const originalText = btn.textContent;
  btn.textContent = 'Verificando...';
  btn.disabled    = true;

  try {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), 12000);

    const r = await fetch(
      url + '/v1/workforce/overview?tenant=' + encodeURIComponent(tenant) + '&days=1',
      { headers: { 'X-Admin-Token': token }, signal: controller.signal }
    );
    clearTimeout(tid);

    if (r.status === 401 || r.status === 403) {
      _showLoginError('Token inválido ou sem permissão. Verifique suas credenciais.');
      return;
    }

    // Salva estado
    state.url    = url;
    state.tenant = tenant;
    state.token  = token;
    localStorage.setItem('wp_url',    url);
    localStorage.setItem('wp_tenant', tenant);
    localStorage.setItem('wp_token',  token);
    _wpTouchSession(true);

    document.getElementById('login').classList.add('hide');

    // Preenche o menu de conta
    _acctSetTenant(tenant, tenant);
    _acctSyncTheme();
    _loadLicenseInfo();

    loadOverview();
    _wpStartIdleWatch();

  } catch (e) {
    if (e.name === 'AbortError') {
      _showLoginError('Servidor não respondeu. Verifique o Endpoint da API.');
    } else {
      _showLoginError('Erro de conexão: ' + e.message);
    }
  } finally {
    btn.textContent = originalText;
    btn.disabled    = false;
  }
}

function logout() {
  _closeAccountMenu();
  localStorage.removeItem('wp_token');
  localStorage.removeItem('wp_last_active');
  if (_wpIdleTimer) { clearInterval(_wpIdleTimer); _wpIdleTimer = null; }
  state.token = '';
  document.getElementById('login').classList.remove('hide');
  _clearLoginError();
}

/* ══════════════════════════════════════════════════════════════════
   BOOTSTRAP
   ══════════════════════════════════════════════════════════════════ */
(function bootstrap() {
  // Aplica tema salvo
  const savedTheme = localStorage.getItem('wp_theme') || 'dark';
  applyTheme(savedTheme);

  // Inicializa menu de conta
  _acctInitMenu();

  // Restaura sessão salva (só se não estiver expirada por inatividade)
  const url    = localStorage.getItem('wp_url');
  const tenant = localStorage.getItem('wp_tenant');
  const token  = localStorage.getItem('wp_token');

  // Prefill de conveniência (sempre): endpoint oculto + tenant
  if (url)    document.getElementById('loginUrl').value    = url;
  if (tenant) document.getElementById('loginTenant').value = tenant;

  if (url && tenant && token && !_wpSessionExpired()) {
    document.getElementById('loginToken').value = token;
    state.url    = url;
    state.tenant = tenant;
    state.token  = token;
    document.getElementById('login').classList.add('hide');
    _wpTouchSession(true);   // renova o carimbo ao retomar

    // Preenche menu de conta
    _acctSetTenant(tenant, tenant);
    _acctSyncTheme();
    _loadLicenseInfo();

    loadOverview();
    _wpStartIdleWatch();
  } else if (token) {
    // Havia sessão salva, mas expirou → exige novo login
    localStorage.removeItem('wp_token');
    localStorage.removeItem('wp_last_active');
    state.token = '';
  }
})();
