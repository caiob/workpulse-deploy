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
  try { fetch(state.url + '/v1/auth/logout', { method: 'POST', credentials: 'include' }); } catch (_) {}
  _resetSessionState();
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
  const email = document.getElementById('loginEmail').value.trim();
  const senha = document.getElementById('loginPassword').value;
  if (!email || !senha) { _showLoginError('Informe e-mail e senha.'); return; }

  const btn = document.querySelector('#login button.primary');
  const originalText = btn.textContent;
  btn.textContent = 'Verificando...'; btn.disabled = true;
  try {
    const r = await fetch(state.url + '/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ email, senha }),
    });
    if (r.status === 423) { _showLoginError('Conta temporariamente bloqueada. Tente novamente em alguns minutos.'); return; }
    if (r.status === 403) { _showLoginError('Conta desativada. Contate o administrador.'); return; }
    if (!r.ok)            { _showLoginError('E-mail ou senha inválidos.'); return; }
    const data = await r.json();
    _applySession(data);
    state.email = email;
    if (data.must_change_pw) { _openChangePassword(true); return; }
    _enterApp();
  } catch (e) {
    _showLoginError('Erro de conexão: ' + e.message);
  } finally {
    btn.textContent = originalText; btn.disabled = false;
  }
}

/* Acesso técnico (break-glass) — Tenant + Token, para suporte/recuperação. */
async function doTechLogin() {
  _clearLoginError();
  const tenant = document.getElementById('loginTenant').value.trim();
  const token  = document.getElementById('loginToken').value.trim();
  if (!tenant || !token) { _showLoginError('Informe Tenant e Token de acesso técnico.'); return; }
  try {
    const r = await fetch(state.url + '/v1/workforce/overview?tenant=' + encodeURIComponent(tenant) + '&days=1',
      { headers: { 'X-Admin-Token': token }, credentials: 'include' });
    if (r.status === 401 || r.status === 403) { _showLoginError('Tenant ou token inválido.'); return; }
    state.authed = true; state.breakGlass = true; state.token = token; state.tenant = tenant;
    state.role = 'admin_master'; state.displayName = 'Acesso técnico'; state.email = 'suporte (token)'; state.mustChangePw = false;
    _wpTouchSession(true);
    _enterApp();
  } catch (e) { _showLoginError('Erro de conexão: ' + e.message); }
}

/* Esqueci minha senha — envia link por e-mail (autoatendimento). */
async function doForgot() {
  const email = document.getElementById('loginEmail').value.trim();
  if (!email) { _showLoginError('Digite seu e-mail no campo acima e clique novamente em “Esqueci minha senha”.'); return; }
  try {
    await fetch(state.url + '/v1/auth/forgot', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'include', body: JSON.stringify({ email }),
    });
  } catch (_) {}
  _showLoginError('Se o e-mail existir, enviaremos um link para redefinir a senha.');
}

/* Mostra/oculta o bloco de acesso técnico no login. */
function toggleTechAccess() {
  const b = document.getElementById('techAccess');
  if (b) b.style.display = (b.style.display === 'none' || !b.style.display) ? 'block' : 'none';
}


async function logout() {
  _closeAccountMenu();
  try { await fetch(state.url + '/v1/auth/logout', { method: 'POST', credentials: 'include' }); } catch (_) {}
  _resetSessionState();
  document.getElementById('login').classList.remove('hide');
  _clearLoginError();
}

function _resetSessionState() {
  state.authed = false; state.breakGlass = false; state.token = '';
  state.role = ''; state.email = ''; state.mustChangePw = false;
  localStorage.removeItem('wp_last_active');
  if (_wpIdleTimer) { clearInterval(_wpIdleTimer); _wpIdleTimer = null; }
}

/* Chamado por wp-core quando uma chamada retorna 401. */
function _handleAuthFail() {
  _resetSessionState();
  const el = document.getElementById('login');
  if (el) el.classList.remove('hide');
  _showLoginError('Sessão expirada. Faça login novamente.');
}

/* ══════════════════════════════════════════════════════════════════
   BOOTSTRAP
   ══════════════════════════════════════════════════════════════════ */
function _applySession(data) {
  state.authed = true; state.breakGlass = false; state.token = '__session__';
  state.tenant = data.tenant || ''; state.role = data.role || '';
  state.displayName = data.display_name || ''; state.mustChangePw = !!data.must_change_pw;
  _wpTouchSession(true);
}

function _enterApp() {
  document.getElementById('login').classList.add('hide');
  _acctSetTenant(state.tenant, state.tenant);
  _acctSetUser();
  _acctSyncTheme();
  _applyRoleUI();
  _loadLicenseInfo();
  loadOverview();
  _wpStartIdleWatch();
}

/* Preenche o nome do usuário logado no menu de conta. */
function _acctSetUser() {
  const el = document.getElementById('acctUserName');
  if (!el) return;
  el.textContent = state.displayName || state.email || '—';
  el.title = state.email || '';
}

/* Mostra/oculta elementos conforme o perfil (data-role="admin_master"). */
function _applyRoleUI() {
  const isAdmin = state.role === 'admin_master' || state.role === 'wp_admin';
  document.querySelectorAll('[data-role="admin_master"]').forEach(el => {
    el.style.display = isAdmin ? '' : 'none';
  });
}

/* ── Política de senha (espelha o backend: 10+ chars, 3 de 4 classes) ── */
function _pwPolicyOk(pw) {
  if (!pw || pw.length < 10) return false;
  let c = 0;
  if (/[a-z]/.test(pw)) c++; if (/[A-Z]/.test(pw)) c++;
  if (/[0-9]/.test(pw)) c++; if (/[^A-Za-z0-9]/.test(pw)) c++;
  return c >= 3;
}

/* ── Troca de senha (primeiro acesso ou voluntária) ── */
function _openChangePassword(forced) {
  const m = document.getElementById('changePw'); if (!m) return;
  const fb = document.getElementById('cpForcedMsg'); if (fb) fb.style.display = forced ? 'block' : 'none';
  const err = document.getElementById('cpError'); if (err) err.style.display = 'none';
  ['cpCurrent','cpNew','cpNew2'].forEach(id => { const e = document.getElementById(id); if (e) e.value = ''; });
  m.dataset.forced = forced ? '1' : '';
  m.classList.remove('hide');
  document.getElementById('login').classList.add('hide');
}

function _closeChangePassword() {
  const m = document.getElementById('changePw');
  if (m && !m.dataset.forced) m.classList.add('hide');
}

async function submitChangePassword() {
  const cur = document.getElementById('cpCurrent').value;
  const n1  = document.getElementById('cpNew').value;
  const n2  = document.getElementById('cpNew2').value;
  const err = document.getElementById('cpError');
  const show = (m) => { err.textContent = m; err.style.display = 'block'; };
  if (n1 !== n2) { show('As senhas não coincidem.'); return; }
  if (!_pwPolicyOk(n1)) { show('A senha deve ter 10+ caracteres e ao menos 3 de 4 tipos: minúscula, maiúscula, número e símbolo.'); return; }
  try {
    await apiFetch('POST', '/v1/auth/password', { senha_atual: cur, senha_nova: n1 });
    state.mustChangePw = false;
    const m = document.getElementById('changePw'); m.dataset.forced = ''; m.classList.add('hide');
    if (!state.authed) { document.getElementById('login').classList.remove('hide'); }
    else { _enterApp(); toast('Senha alterada com sucesso.'); }
  } catch (e) { show('Não foi possível alterar: ' + (e.message || 'senha atual incorreta')); }
}

/* ── Definir senha via link de e-mail (?action=set-password) ── */
function _openSetPassword(token) {
  const m = document.getElementById('setPw'); if (!m) return;
  m.dataset.token = token; m.classList.remove('hide');
  document.getElementById('login').classList.add('hide');
}

async function submitSetPassword() {
  const m  = document.getElementById('setPw');
  const n1 = document.getElementById('spNew').value;
  const n2 = document.getElementById('spNew2').value;
  const err = document.getElementById('spError');
  const show = (x) => { err.textContent = x; err.style.display = 'block'; };
  if (n1 !== n2) { show('As senhas não coincidem.'); return; }
  if (!_pwPolicyOk(n1)) { show('Senha fraca: 10+ caracteres e 3 de 4 tipos.'); return; }
  try {
    const r = await fetch(state.url + '/v1/auth/set-password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'include', body: JSON.stringify({ token: m.dataset.token, senha: n1 }),
    });
    if (!r.ok) { const t = await r.text(); show('Falha: ' + t); return; }
    m.classList.add('hide');
    history.replaceState({}, '', location.pathname);
    document.getElementById('login').classList.remove('hide');
    _showLoginError('Senha definida! Faça login com sua nova senha.');
  } catch (e) { show('Erro: ' + e.message); }
}

(function bootstrap() {
  const savedTheme = localStorage.getItem('wp_theme') || 'dark';
  applyTheme(savedTheme);
  _acctInitMenu();

  // Link de redefinição/definição de senha
  const params = new URLSearchParams(location.search);
  if (params.get('action') === 'set-password' && params.get('token')) {
    _openSetPassword(params.get('token'));
    return;
  }

  // Restaura sessão por cookie (GET /v1/auth/me)
  (async () => {
    try {
      const r = await fetch(state.url + '/v1/auth/me', { credentials: 'include' });
      if (!r.ok) throw 0;
      const me = await r.json();
      if (_wpSessionExpired()) {
        try { await fetch(state.url + '/v1/auth/logout', { method: 'POST', credentials: 'include' }); } catch (_) {}
        throw 0;
      }
      state.authed = true; state.breakGlass = false; state.token = '__session__';
      state.tenant = me.tenant || ''; state.role = me.role || ''; state.displayName = me.display_name || '';
      state.email = me.email || '';
      _wpTouchSession(true);
      _enterApp();
    } catch (_) {
      document.getElementById('login').classList.remove('hide');
    }
  })();
})();
