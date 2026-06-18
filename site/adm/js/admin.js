
const state = {
  url:    localStorage.getItem('wp_admin_url')   || 'https://api.workpulse.com.br',
  token:  localStorage.getItem('wp_admin_token') || '',
  charts: {},
  tenants: [], licenses: [], audit: [], stats: null,
};

// ---------- Tema (claro / escuro) ----------
function applyTheme(theme) {
  document.body.classList.toggle('theme-light', theme === 'light');
  _admSyncTheme();
  // Reconstrói os gráficos com as cores do tema atual
  Object.keys(state.charts).forEach(k => {
    try { state.charts[k] && state.charts[k].destroy && state.charts[k].destroy(); } catch (_) {}
    delete state.charts[k];
  });
  if (state.token) {
    const active  = document.querySelector('nav.tabs button.active');
    const t       = active && active.dataset.tab;
    const loaders = { overview: loadOverview, tenants: loadTenantsLicenses, system: loadSystem, audit: loadAudit, logins: loadLogins };
    if (t && loaders[t]) loaders[t]();
  }
}
function toggleTheme() {
  const next = document.body.classList.contains('theme-light') ? 'dark' : 'light';
  localStorage.setItem('wp_admin_theme', next);
  applyTheme(next);
}
function _admSyncTheme() {
  const isLight = document.body.classList.contains('theme-light');
  const chk = document.getElementById('admThemeCheck');
  const lbl = document.getElementById('admThemeLbl');
  if (chk) chk.checked = isLight;
  if (lbl) lbl.textContent = isLight ? 'Claro' : 'Escuro';
}

// ---------- Menu do operador (canto sup. direito) ----------
function _admToggleMenu(e) {
  if (e) e.stopPropagation();
  const w = document.getElementById('admWrap');
  if (w) w.classList.toggle('open', !w.classList.contains('open'));
}
function _admCloseMenu() { const w = document.getElementById('admWrap'); if (w) w.classList.remove('open'); }
function _admInitMenu() {
  const t = document.getElementById('admTrigger');
  if (!t) return;
  t.addEventListener('click', _admToggleMenu);
  const dd = document.getElementById('admDropdown');
  if (dd) dd.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', _admCloseMenu);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') _admCloseMenu(); });
}

// ---------- Controle de sessão — expira por inatividade (30 min) ----------
const WP_ADMIN_IDLE_MS = 30 * 60 * 1000;
let _admIdleTimer = null, _admLastWrite = 0, _admWatchInit = false;
function _admTouchSession(force) {
  const now = Date.now();
  if (force || now - _admLastWrite > 20000) {
    localStorage.setItem('wp_admin_last_active', String(now));
    _admLastWrite = now;
  }
}
function _admSessionExpired() {
  const la = parseInt(localStorage.getItem('wp_admin_last_active') || '0', 10);
  return !la || (Date.now() - la) > WP_ADMIN_IDLE_MS;
}
function _admExpireSession() {
  localStorage.removeItem('wp_admin_token');
  localStorage.removeItem('wp_admin_last_active');
  state.token = '';
  if (_admIdleTimer) { clearInterval(_admIdleTimer); _admIdleTimer = null; }
  _admCloseMenu();
  const tk = document.getElementById('loginToken');
  if (tk) tk.value = '';
  document.getElementById('login').classList.remove('hide');
  _admShowLoginError('Sessão expirada por inatividade. Autentique novamente.');
}
function _admStartIdleWatch() {
  if (!_admWatchInit) {
    ['mousedown', 'keydown', 'scroll', 'touchstart', 'visibilitychange'].forEach(ev =>
      window.addEventListener(ev, () => { if (state.token) _admTouchSession(false); }, { passive: true }));
    _admWatchInit = true;
  }
  if (_admIdleTimer) clearInterval(_admIdleTimer);
  _admIdleTimer = setInterval(() => { if (state.token && _admSessionExpired()) _admExpireSession(); }, 30000);
}
function _admShowLoginError(msg) { const el = document.getElementById('loginError'); if (el) { el.textContent = msg; el.style.display = 'block'; } }
function _admClearLoginError() { const el = document.getElementById('loginError'); if (el) el.style.display = 'none'; }

// ---------- Auth ----------
async function doLogin() {
  _admClearLoginError();
  const url   = document.getElementById('loginUrl').value.trim().replace(/\/$/, '');
  const token = document.getElementById('loginToken').value.trim();
  if (!url || !token) { _admShowLoginError('Preencha o endpoint e o token.'); return; }

  const btn  = document.querySelector('#login button.primary');
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.textContent = 'Verificando...'; btn.disabled = true; }
  try {
    // Valida o token contra um endpoint protegido ANTES de entrar.
    const r = await fetch(url + '/v1/admin/stats', { headers: { 'X-Admin-Token': token } });
    if (r.status === 401 || r.status === 403) { _admShowLoginError('Token inválido. Verifique o X-Admin-Token.'); return; }
    if (!r.ok) { _admShowLoginError('Falha ao validar (HTTP ' + r.status + ').'); return; }
  } catch (e) {
    _admShowLoginError('Erro de conexão com o servidor: ' + e.message); return;
  } finally {
    if (btn) { btn.textContent = orig; btn.disabled = false; }
  }

  state.url = url; state.token = token;
  localStorage.setItem('wp_admin_url', url);
  localStorage.setItem('wp_admin_token', token);
  _admTouchSession(true);
  document.getElementById('login').classList.add('hide');
  _admStartIdleWatch();
  loadAll();
}

// Carrega o painel inicial (aba Visão). Demais abas carregam ao clicar.
function loadAll() {
  loadOverview();
}
function logout() {
  _admCloseMenu();
  localStorage.removeItem('wp_admin_token');
  localStorage.removeItem('wp_admin_last_active');
  if (_admIdleTimer) { clearInterval(_admIdleTimer); _admIdleTimer = null; }
  state.token = '';
  document.getElementById('login').classList.remove('hide');
}

// ---------- Utils ----------
function toast(msg, ms=2800) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.remove('hide');
  setTimeout(() => el.classList.add('hide'), ms);
}
function fmtBytes(b) {
  if (!b) return '0';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(2) + ' MB';
}
function fmtNum(n) { return (n||0).toLocaleString('pt-BR'); }
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
}
function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' }) + ' ' +
         d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function relTime(iso) {
  if (!iso) return '—';
  const sec = (Date.now() - new Date(iso)) / 1000;
  if (sec < 60) return 'agora';
  if (sec < 3600) return Math.floor(sec/60)+'min';
  if (sec < 86400) return Math.floor(sec/3600)+'h';
  return Math.floor(sec/86400)+'d';
}
function planBadge(p) {
  const cls = p === 'enterprise' ? 'primary' : p === 'business' ? 'ok' : 'muted';
  return `<span class="badge ${cls}">${p||'—'}</span>`;
}

async function api(path, opts={}) {
  const r = await fetch(state.url + path, {
    headers: { 'Content-Type': 'application/json', 'X-Admin-Token': state.token, ...(opts.headers||{}) },
    ...opts,
  });
  if (!r.ok) {
    const t = await r.text().catch(()=>r.statusText);
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}

// ---------- Tabs ----------
document.querySelectorAll('nav.tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav.tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    ['overview','tenants','system','audit','logins'].forEach(t =>
      document.getElementById('tab-'+t).classList.add('hide'));
    document.getElementById('tab-'+btn.dataset.tab).classList.remove('hide');
    if (btn.dataset.tab === 'overview') loadOverview();
    if (btn.dataset.tab === 'tenants')  loadTenantsLicenses();
    if (btn.dataset.tab === 'system')   loadSystem();
    if (btn.dataset.tab === 'audit')    loadAudit();
    if (btn.dataset.tab === 'logins')   loadLogins();
  });
});

// ---------- Overview ----------
async function loadOverview() {
  try {
    const [stats, tenants, licenses] = await Promise.all([
      api('/v1/admin/stats'),
      api('/v1/admin/tenants'),
      api('/v1/admin/licenses?status=active'),
    ]);
    state.stats = stats;
    state.tenants = tenants.tenants || [];
    state.licenses = licenses.licenses || [];

    document.getElementById('kTenants').textContent     = fmtNum(stats.counts.tenants);
    document.getElementById('kTenantsSub').textContent  = `${fmtNum(stats.counts.devices)} devices · ${fmtNum(stats.counts.users)} usuários`;
    document.getElementById('kUsers').textContent       = fmtNum(stats.counts.users);
    document.getElementById('kUsersSub').textContent    = `em ${fmtNum(stats.counts.tenants)} tenants`;
    document.getElementById('kLicActive').textContent   = fmtNum(stats.counts.active_licenses);
    document.getElementById('kLicSub').textContent      = `${fmtNum(stats.counts.revoked_licenses)} revogadas`;
    document.getElementById('kEvents24h').textContent   = fmtNum(stats.events_last_24h);
    document.getElementById('kEventsSub').textContent   = `${fmtNum(stats.events_last_7d)} nos últimos 7d`;

    document.getElementById('hVersion').textContent  = stats.version;
    document.getElementById('hDb').textContent       = fmtBytes(stats.db_size_bytes);
    document.getElementById('hEvents7d').textContent = fmtNum(stats.events_last_7d);
    document.getElementById('serverVersion').textContent = `v${stats.version}`;

    const ratePerHour = stats.events_last_24h / 24;
    document.getElementById('ingestRate').textContent = `~${ratePerHour.toFixed(1)} ev/h`;

    drawIngestChart('chartIngest', stats.ingest_per_hour || []);
    drawOSDist('osDist', stats.os_distribution || []);
    document.getElementById('osTotal').textContent =
      `${fmtNum((stats.os_distribution||[]).reduce((a,o)=>a+o.n,0))} devices`;

    // Top tenants by users
    const tt = [...state.tenants].sort((a,b) => (b.users||0) - (a.users||0)).slice(0,6);
    document.querySelector('#topTenantsTable tbody').innerHTML = tt.map(t => `
      <tr>
        <td><strong>${t.name}</strong><br><span class="mono" style="color:var(--text-muted);font-size:11px">${t.id}</span></td>
        <td>${planBadge(t.plan)}</td>
        <td class="num">${fmtNum(t.users)}</td>
        <td class="num">${fmtNum(t.active_licenses)}</td>
        <td>${relTime(t.last_activity)}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="empty">Sem tenants registrados</td></tr>';

    // Expiring soon
    const expiring = state.licenses
      .filter(l => l.days_remaining !== null && l.days_remaining <= 30 && !l.expired && !l.revoked)
      .sort((a,b) => a.days_remaining - b.days_remaining)
      .slice(0,8);
    document.querySelector('#expiringTable tbody').innerHTML = expiring.map(l => `
      <tr>
        <td class="mono">${l.lic_id.slice(0,12)}…</td>
        <td>${l.tenant_name || l.tenant_id}</td>
        <td class="num"><span class="badge ${l.days_remaining < 7 ? 'danger' : 'warn'}">${l.days_remaining}d</span></td>
        <td class="num">${l.seats}</td>
      </tr>`).join('') || '<tr><td colspan="4" class="empty">Nada expirando — tudo em dia.</td></tr>';

    document.getElementById('healthLed').className = 'led';
    document.getElementById('healthText').textContent = 'healthy';
  } catch (e) {
    document.getElementById('healthLed').className = 'led danger';
    document.getElementById('healthText').textContent = 'erro';
    toast('Falha: '+e.message);
  }
}

function drawIngestChart(canvasId, hourly) {
  const _ic = document.body.classList.contains('theme-light')
    ? { tick: '#4a5578', grid: 'rgba(160,180,210,.5)' }
    : { tick: '#7d8ab9', grid: 'rgba(31,44,94,.5)' };
  const labels = hourly.map(h => h.hour.slice(11,13)+'h');
  const values = hourly.map(h => h.events);
  if (state.charts[canvasId]) state.charts[canvasId].destroy();
  state.charts[canvasId] = new Chart(document.getElementById(canvasId).getContext('2d'), {
    type: 'bar',
    data: { labels, datasets: [{
      label: 'Eventos', data: values,
      backgroundColor: 'rgba(108,140,255,.75)',
      borderRadius: 3,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: _ic.tick, font: { family: 'JetBrains Mono', size: 10 } }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: _ic.tick, font: { family: 'JetBrains Mono', size: 10 } }, grid: { color: _ic.grid } }
      }
    }
  });
}

function drawOSDist(elId, dist) {
  const total = dist.reduce((a,d) => a + d.n, 0) || 1;
  document.getElementById(elId).innerHTML = dist.map(d => {
    const pct = Math.round(d.n/total*100);
    return `<div class="dist-row">
      <div class="k">${d.os}</div>
      <div class="v">${d.n} <small style="color:var(--text-muted)">(${pct}%)</small></div>
      <div class="dist-bar"><span style="width:${pct}%"></span></div>
    </div>`;
  }).join('') || '<div class="empty">sem dados</div>';
}

// ---------- Tenants ----------
// Carrega tenants + licenças juntos e renderiza a visão consolidada.
async function loadTenantsLicenses() {
  try {
    const [t, l] = await Promise.all([
      api('/v1/admin/tenants'),
      api('/v1/admin/licenses'),
    ]);
    state.tenants  = t.tenants  || [];
    state.licenses = l.licenses || [];
    renderTenantsLicenses();
  } catch (e) { toast('Erro: '+e.message); }
}

function _licStatus(l) {
  if (l.revoked) return 'revoked';
  if (l.expired) return 'expired';
  if (l.days_remaining !== null && l.days_remaining <= 30) return 'expiring';
  return 'active';
}
function _licBadge(l) {
  return l.revoked ? '<span class="badge danger">revogada</span>'
    : l.expired ? '<span class="badge muted">expirada</span>'
    : (l.days_remaining !== null && l.days_remaining <= 30) ? `<span class="badge warn">expira ${l.days_remaining}d</span>`
    : '<span class="badge ok">ativa</span>';
}

// Cor de realce da validade (vermelho=vencida/revogada, âmbar=<=30d).
function _licColor(l) {
  if (l.revoked || l.expired) return 'var(--danger)';
  if (l.days_remaining !== null && l.days_remaining <= 30) return '#f59e0b';
  return '';
}

// Expande / recolhe a linha de licenças de uma empresa.
function _tlToggle(id) {
  const d = document.querySelector('.tl-detail[data-detail="' + id + '"]');
  if (!d) return;
  d.classList.toggle('hide');
  const hidden = d.classList.contains('hide');
  const a = document.getElementById('tlArrow-' + id);
  if (a) a.classList.toggle('open', !hidden);
}

function renderTenantsLicenses() {
  const q      = (document.getElementById('tlSearch').value || '').toLowerCase();
  const plan   = document.getElementById('tlPlanFilter').value;
  const status = document.getElementById('tlStatusFilter').value;
  const all    = state.licenses;

  // KPIs globais de licença
  const active  = all.filter(l => !l.revoked && !l.expired).length;
  const expSoon = all.filter(l => l.days_remaining !== null && l.days_remaining <= 30 && !l.expired && !l.revoked).length;
  const dead    = all.filter(l => l.revoked || l.expired).length;
  document.getElementById('tlTotal').textContent   = fmtNum(all.length);
  document.getElementById('tlActive').textContent  = fmtNum(active);
  document.getElementById('tlExpSoon').textContent = fmtNum(expSoon);
  document.getElementById('tlDead').textContent    = fmtNum(dead);

  // Agrupa as licenças por tenant
  const byTenant = {};
  all.forEach(l => { (byTenant[l.tenant_id] = byTenant[l.tenant_id] || []).push(l); });

  const rows = state.tenants.filter(t => {
    if (q && !(t.id.toLowerCase().includes(q) || (t.name||'').toLowerCase().includes(q))) return false;
    if (plan && t.plan !== plan) return false;
    if (status !== 'all' && !(byTenant[t.id] || []).some(l => _licStatus(l) === status)) return false;
    return true;
  });

  document.getElementById('tlCount').textContent =
    `${rows.length} de ${state.tenants.length} empresas · ${all.length} licenças`;

  // Com busca ou filtro de status ativo, já abre as linhas relevantes.
  const autoExpand = status !== 'all' || !!q;

  document.querySelector('#tlTable tbody').innerHTML = rows.length ? rows.map(t => {
    const lics = (byTenant[t.id] || []).slice().sort((a, b) => {
      const da = a.days_remaining === null ? 1e9 : a.days_remaining;
      const db = b.days_remaining === null ? 1e9 : b.days_remaining;
      return da - db;
    });
    const shown   = status === 'all' ? lics : lics.filter(l => _licStatus(l) === status);
    const activeN = lics.filter(l => !l.revoked && !l.expired).length;
    const open    = autoExpand;

    const detail = `
      <tr class="tl-detail ${open ? '' : 'hide'}" data-detail="${t.id}">
        <td colspan="12" class="tl-detail-cell">
          ${shown.length ? `
          <table class="tl-sub">
            <thead><tr><th>License ID</th><th>Plano</th><th class="num">Seats</th><th>Emitida</th><th>Expira</th><th class="num">Dias</th><th>Status</th><th></th></tr></thead>
            <tbody>
              ${shown.map(l => `
                <tr>
                  <td class="mono">${l.lic_id}</td>
                  <td>${planBadge(l.plan)}</td>
                  <td class="num">${fmtNum(l.seats)}</td>
                  <td>${fmtDate(l.issued)}</td>
                  <td style="color:${_licColor(l)}">${fmtDate(l.expires)}</td>
                  <td class="num mono" style="color:${_licColor(l)}">${l.days_remaining !== null ? l.days_remaining + 'd' : '—'}</td>
                  <td>${_licBadge(l)}</td>
                  <td style="white-space:nowrap">${l.revoked ? '' : `<button class="ghost" onclick="event.stopPropagation();licEdit('${l.lic_id}')">editar</button> <button class="ghost" onclick="event.stopPropagation();revokeLic('${l.lic_id}')">revogar</button>`}</td>
                </tr>`).join('')}
            </tbody>
          </table>` : `<div class="empty" style="padding:14px">Esta empresa não tem licenças${status !== 'all' ? ' nesse filtro' : ''}.</div>`}
        </td>
      </tr>`;

    return `
      <tr class="tl-main" onclick="_tlToggle('${t.id}')">
        <td class="tl-caret"><span class="tl-arrow ${open ? 'open' : ''}" id="tlArrow-${t.id}">▶</span></td>
        <td class="mono">${t.id}</td>
        <td><strong>${t.name}</strong></td>
        <td>${planBadge(t.plan)}</td>
        <td><span class="badge ${t.status === 'active' ? 'ok' : 'muted'}">${t.status}</span></td>
        <td class="num">${fmtNum(t.users)}</td>
        <td class="num">${fmtNum(t.devices)}</td>
        <td class="num">${activeN}<span style="color:var(--text-muted)">/${lics.length}</span></td>
        <td>${fmtDate(t.created_at)}</td>
        <td>${relTime(t.last_activity)}</td>
        <td class="num" onclick="event.stopPropagation()">
          <span style="color:var(--text-dim);font-size:12px">${t.retention_days != null ? t.retention_days + 'd' : '90d ⚙'}</span>
          <button class="ghost" style="margin-left:4px;font-size:11px;padding:2px 6px" onclick="setRetention('${t.id}', ${t.retention_days || 90})">✎</button>
        </td>
        <td onclick="event.stopPropagation()" style="white-space:nowrap"><button class="ghost" onclick="genTenantAccess('${t.id}')">gerar acesso</button> <button class="ghost" onclick="finOpen('${t.id}')">financeiro</button></td>
      </tr>
      ${detail}`;
  }).join('') : `<tr><td colspan="12" class="empty">Nenhuma empresa encontrada.</td></tr>`;
}

// ---------- Criar empresa ----------
async function createCompany() {
  const id    = document.getElementById('ncId').value.trim();
  const name  = document.getElementById('ncName').value.trim();
  const plan  = document.getElementById('ncPlan').value;
  const seats = parseInt(document.getElementById('ncSeats').value, 10) || 1;
  const days  = parseInt(document.getElementById('ncDays').value, 10) || 365;
  if (!id || !name) { toast('Preencha o ID e o nome da empresa'); return; }
  try {
    const r = await api('/v1/license/issue', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: id, tenant_name: name,
        plan: plan, seats: seats, days_valid: days,
      }),
    });
    document.getElementById('ncResTitle').textContent = 'EMPRESA CRIADA';
    document.getElementById('ncLicBlock').classList.remove('hide');
    document.getElementById('ncLicKey').value = r.license_key || '';
    document.getElementById('ncResTenant').textContent = r.tenant_id || id;
    document.getElementById('ncDashToken').value = r.dash_token || '';
    document.getElementById('ncResult').classList.remove('hide');
    document.getElementById('ncId').value = '';
    document.getElementById('ncName').value = '';
    toast('Empresa "' + name + '" criada.');
    loadTenantsLicenses();
  } catch (e) {
    toast('Erro: ' + e.message);
  }
}

// Gera/renova o acesso ao painel de um tenant existente.
async function genTenantAccess(tenantId) {
  if (!confirm('Gerar um novo token de acesso ao painel para "' + tenantId + '"?\n' +
               'Se já existia um, o anterior deixará de funcionar.')) return;
  try {
    const r = await api('/v1/admin/tenant-token?tenant=' + encodeURIComponent(tenantId), { method: 'POST' });
    document.getElementById('ncResTitle').textContent = 'ACESSO AO PAINEL GERADO';
    document.getElementById('ncLicBlock').classList.add('hide');
    document.getElementById('ncResTenant').textContent = r.tenant_id;
    document.getElementById('ncDashToken').value = r.dash_token || '';
    document.getElementById('ncResult').classList.remove('hide');
    document.getElementById('ncResult').scrollIntoView({ behavior: 'smooth', block: 'center' });
    toast('Acesso gerado para ' + tenantId);
  } catch (e) {
    toast('Erro: ' + e.message);
  }
}

async function setRetention(tenantId, currentDays) {
  const input = prompt(
    `Retenção de dados para "${tenantId}" (em dias).\nDigite um número entre 1 e 3650.\nAtual: ${currentDays} dias`,
    currentDays
  );
  if (input === null) return;  // cancelled
  const days = parseInt(input, 10);
  if (!days || days < 1 || days > 3650) { toast('Valor inválido. Use entre 1 e 3650.'); return; }
  try {
    await api(`/v1/admin/tenants/${encodeURIComponent(tenantId)}/retention?days=${days}`, { method: 'PUT' });
    toast(`Retenção de ${tenantId} definida para ${days} dias.`);
    loadTenantsLicenses();
  } catch (e) { toast('Erro: ' + e.message); }
}

function copyLicKey()   { copyFrom('ncLicKey', 'Licença copiada'); }
function copyDashToken(){ copyFrom('ncDashToken', 'Token do painel copiado'); }
function copyFrom(elId, okMsg) {
  const ta = document.getElementById(elId);
  ta.select();
  if (navigator.clipboard) {
    navigator.clipboard.writeText(ta.value)
      .then(() => toast(okMsg))
      .catch(() => toast('Selecione o texto e copie com Ctrl+C'));
  } else {
    document.execCommand('copy');
    toast(okMsg);
  }
}

// ---------- Licenças (ações) ----------
async function revokeLic(licId) {
  if (!confirm(`Revogar licença ${licId}? Esta ação não pode ser desfeita.`)) return;
  try {
    await api('/v1/license/revoke?lic_id=' + encodeURIComponent(licId), { method: 'POST' });
    toast('Licença revogada: ' + licId);
    loadTenantsLicenses();
  } catch (e) { toast('Erro: '+e.message); }
}

// ---------- System ----------
async function loadSystem() {
  try {
    const stats = await api('/v1/admin/stats');
    document.getElementById('sysVersion').textContent = `v${stats.version}`;
    document.getElementById('sysTime').textContent = fmtDateTime(stats.server_time);

    // Clock drift
    const local = new Date();
    const remote = new Date(stats.server_time);
    const drift = Math.abs(local - remote) / 1000;
    const driftEl = document.getElementById('sysClockDrift');
    driftEl.textContent = `drift: ${drift.toFixed(1)}s`;
    if (drift > 60) driftEl.classList.add('down'); else driftEl.classList.add('up');

    document.getElementById('sysDb').innerHTML = `${(stats.db_size_bytes/1048576).toFixed(2)}<small>MB</small>`;
    drawIngestChart('chartIngest2', stats.ingest_per_hour || []);

    // Plan distribution
    const planTotal = stats.plan_distribution.reduce((a,p)=>a+p.n,0) || 1;
    document.getElementById('planDist').innerHTML = stats.plan_distribution.map(p => {
      const pct = Math.round(p.n/planTotal*100);
      return `<div class="dist-row">
        <div class="k">${p.plan}</div>
        <div class="v">${p.n} <small style="color:var(--text-muted)">(${pct}%)</small></div>
        <div class="dist-bar"><span style="width:${pct}%"></span></div>
      </div>`;
    }).join('') || '<div class="empty">sem dados</div>';

    drawOSDist('osDist2', stats.os_distribution || []);

    // Counters
    document.getElementById('cntTenants').textContent    = fmtNum(stats.counts.tenants);
    document.getElementById('cntUsers').textContent      = fmtNum(stats.counts.users);
    document.getElementById('cntDevices').textContent    = fmtNum(stats.counts.devices);
    document.getElementById('cntLicActive').textContent  = fmtNum(stats.counts.active_licenses);
    document.getElementById('cntLicRevoked').textContent = fmtNum(stats.counts.revoked_licenses);
    document.getElementById('cntEvents').textContent     = fmtNum(stats.counts.total_events);
    loadAwsCosts();
    loadTenantCosts();
    runSim();
    loadPlanPrices();
    loadAgentConfig();
    loadReleases();
  } catch (e) { toast('Erro: '+e.message); }
}

// ---------- Builds do agente (releases) ----------
function _relBytes(n) {
  if (!n) return '—';
  if (n >= 1048576) return (n/1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n/1024).toFixed(0) + ' KB';
  return n + ' B';
}

async function loadReleases() {
  const box = document.getElementById('relList');
  if (!box) return;
  try {
    const d = await api('/v1/admin/releases');
    const list = d.releases || [];
    if (!list.length) { box.innerHTML = '<p style="color:var(--text-muted)">Nenhum build enviado ainda.</p>'; return; }
    const _latest = '<span style="background:rgba(47,111,237,.15);color:#2f6fed;border:1px solid rgba(47,111,237,.4);border-radius:10px;padding:1px 7px;font-size:10px;font-weight:600;margin-left:4px">última</span>';
    box.innerHTML = '<table><thead><tr><th>Versão</th><th>SO</th><th>Canal</th><th>Arquivo</th><th>Tamanho</th><th>SHA-256</th><th>Enviado</th><th></th></tr></thead><tbody>' +
      list.map(r => `<tr>
        <td><strong>${r.version}</strong>${r.is_latest ? _latest : ''}</td>
        <td>${r.os}${r.arch && r.arch!=='amd64' ? ' · '+r.arch : ''}</td>
        <td>${r.channel === 'generic' ? 'Genérico' : (r.channel||'')}</td>
        <td title="${r.filename||''}">${r.filename||'—'}</td>
        <td>${_relBytes(r.size_bytes)}</td>
        <td><code style="font-size:11px">${(r.sha256||'').slice(0,12)}…</code></td>
        <td style="font-size:12px">${r.created_at ? new Date(r.created_at).toLocaleString('pt-BR') : '—'}</td>
        <td style="white-space:nowrap">
          <button class="ghost" onclick="downloadRelease(${r.id})">Baixar</button>
          <button class="ghost" onclick="copyReleaseLink(${r.id})" title="Gera um link de download válido por 24h para enviar ao suporte / EndpointCentral">Copiar link</button>
          ${r.is_latest ? '' : `<button class="ghost" onclick="promoteRelease(${r.id})">Tornar última</button>`}
          <button class="ghost" style="color:var(--danger)" onclick="deleteRelease(${r.id},'${(r.version||'').replace(/'/g,'')}')">Excluir</button>
        </td>
      </tr>`).join('') + '</tbody></table>';
  } catch (e) { box.innerHTML = '<p style="color:var(--text-muted)">Erro: '+e.message+'</p>'; }
}

async function uploadRelease() {
  const fileEl = document.getElementById('relFile');
  const f = fileEl && fileEl.files && fileEl.files[0];
  const msg = document.getElementById('relMsg');
  if (!f) { if (msg) msg.textContent = 'Selecione um arquivo (.msi/.deb).'; return; }
  const os      = document.getElementById('relOs').value;
  const version = document.getElementById('relVersion').value.trim();
  const channel = document.getElementById('relChannel').value;
  const tenant  = document.getElementById('relTenant').value.trim();
  const notes   = document.getElementById('relNotes').value.trim();
  if (!/^\d+\.\d+\.\d+$/.test(version)) { if (msg) msg.textContent = 'Versão inválida (use X.Y.Z).'; return; }
  if (channel === 'tenant' && !tenant)  { if (msg) msg.textContent = 'Informe o tenant para canal por cliente.'; return; }

  const qs = new URLSearchParams({ os, version, filename: f.name, channel: channel === 'tenant' ? 'tenant' : 'generic' });
  if (channel === 'tenant') qs.set('tenant_id', tenant);
  if (notes) qs.set('notes', notes);

  if (msg) msg.textContent = 'Enviando ' + f.name + '…';
  try {
    const buf = await f.arrayBuffer();
    const r = await fetch(state.url + '/v1/admin/releases?' + qs.toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Admin-Token': state.token },
      body: buf,
    });
    if (!r.ok) { const t = await r.text().catch(()=>r.statusText); throw new Error(`${r.status}: ${t}`); }
    if (msg) msg.textContent = 'Build enviado.';
    if (fileEl) fileEl.value = '';
    loadReleases();
  } catch (e) { if (msg) msg.textContent = 'Falha: ' + e.message; }
}

async function promoteRelease(id) {
  try { await api('/v1/admin/releases/' + id + '/promote', { method: 'PUT' }); loadReleases(); }
  catch (e) { toast('Falha: ' + e.message); }
}

async function downloadRelease(id) {
  try {
    const d = await api('/v1/admin/releases/' + id + '/download');
    if (d.url) window.open(d.url, '_blank');
  } catch (e) { toast('Falha ao gerar link: ' + e.message); }
}

async function copyReleaseLink(id) {
  try {
    const d = await api('/v1/admin/releases/' + id + '/download?expires=86400');  // 24h
    if (d.url && navigator.clipboard) {
      await navigator.clipboard.writeText(d.url);
      toast('Link copiado (válido por 24h).');
    } else if (d.url) {
      window.prompt('Copie o link (válido 24h):', d.url);
    }
  } catch (e) { toast('Falha ao gerar link: ' + e.message); }
}

async function deleteRelease(id, version) {
  if (!confirm('Excluir o build ' + version + '? O arquivo sai do S3 e do histórico.')) return;
  try { await api('/v1/admin/releases/' + id, { method: 'DELETE' }); loadReleases(); }
  catch (e) { toast('Falha: ' + e.message); }
}

function _relChannelToggle() {
  const ch = document.getElementById('relChannel').value;
  const t  = document.getElementById('relTenantWrap');
  if (t) t.style.display = (ch === 'tenant') ? '' : 'none';
}

async function loadAgentConfig() {
  try {
    const d = await api('/v1/admin/agent-config');
    const g = d.global || {};
    document.getElementById('acAfk').value    = g.afk_timeout_sec ?? 180;
    document.getElementById('acPoll').value   = g.poll_interval_ms ?? 1000;
    document.getElementById('acHb').value     = g.heartbeat_interval_sec ?? 5;
    document.getElementById('acTitles').value = (g.capture_titles === false) ? 'false' : 'true';
    document.getElementById('acMeet').value   = (g.meeting_patterns || []).join('\n');
  } catch (e) { /* silencioso */ }
}

async function saveAgentConfig() {
  const msg = document.getElementById('acMsg');
  const body = {
    afk_timeout_sec:        parseInt(document.getElementById('acAfk').value, 10) || 180,
    poll_interval_ms:       parseInt(document.getElementById('acPoll').value, 10) || 1000,
    heartbeat_interval_sec: parseInt(document.getElementById('acHb').value, 10),
    capture_titles:         document.getElementById('acTitles').value === 'true',
    meeting_patterns:       document.getElementById('acMeet').value.split('\n').map(s => s.trim()).filter(Boolean),
  };
  if (isNaN(body.heartbeat_interval_sec)) body.heartbeat_interval_sec = 5;
  try {
    await api('/v1/admin/agent-config', { method: 'PUT', body: JSON.stringify(body) });
    if (msg) { msg.textContent = 'Config global salva.'; setTimeout(() => msg.textContent = '', 2500); }
    toast('Config do agente (global) atualizada.');
  } catch (e) { if (msg) msg.textContent = 'Falha: ' + e.message; }
}

// ===========================================================================
// Financeiro — preços por plano + billing/pagamentos por tenant
// ===========================================================================
function brl(cents) {
  return 'R$ ' + ((cents || 0) / 100).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function loadPlanPrices() {
  try {
    const d = await api('/v1/admin/plan-prices');
    const p = d.prices || {};
    const set = (id, c) => { const el = document.getElementById(id); if (el) el.value = ((c || 0) / 100).toFixed(2); };
    set('ppStarter', p.starter); set('ppBusiness', p.business); set('ppEnterprise', p.enterprise);
  } catch (e) { /* silencioso */ }
}

async function savePlanPrices() {
  const msg = document.getElementById('ppMsg');
  const cents = (id) => Math.round(parseFloat(document.getElementById(id).value || '0') * 100);
  try {
    await api('/v1/admin/plan-prices', {
      method: 'PUT',
      body: JSON.stringify({ prices: { starter: cents('ppStarter'), business: cents('ppBusiness'), enterprise: cents('ppEnterprise') } }),
    });
    if (msg) { msg.textContent = 'Preços salvos.'; setTimeout(() => msg.textContent = '', 2500); }
    toast('Preços dos planos atualizados.');
  } catch (e) { if (msg) msg.textContent = 'Falha: ' + e.message; }
}

let _finTenant = null, _finSeats = 0, _finPlan = '', _finPlanCents = 0;

async function finOpen(tenantId) {
  _finTenant = tenantId;
  const t = (state.tenants || []).find(x => x.id === tenantId);
  document.getElementById('finTenantLbl').textContent = tenantId;
  document.getElementById('finSub').textContent = t ? (t.name + ' (' + tenantId + ')') : tenantId;
  document.getElementById('finError').style.display = 'none';
  document.getElementById('finModal').classList.remove('hide');
  try {
    const d = await api('/v1/admin/tenant-billing?tenant=' + encodeURIComponent(tenantId));
    _finSeats = d.seats || 0; _finPlan = d.plan || ''; _finPlanCents = d.plan_price_cents || 0;
    document.getElementById('finOverride').value = (d.seat_price_cents != null) ? (d.seat_price_cents / 100).toFixed(2) : '';
    document.getElementById('finDiscount').value = d.discount_pct || 0;
    finRenderCalc(d.effective_seat_cents, d.monthly_cents);
    finRenderPayments(d.payments || []);
  } catch (e) {
    document.getElementById('finError').textContent = 'Falha ao carregar: ' + e.message;
    document.getElementById('finError').style.display = 'block';
  }
}

function finRenderCalc(effCents, monthlyCents) {
  const el = document.getElementById('finCalc');
  if (!el) return;
  el.innerHTML = `Plano <strong>${escHtml(_finPlan || '—')}</strong> · preço base do plano ${brl(_finPlanCents)}/seat`
    + ` · preço efetivo <strong>${brl(effCents)}</strong>/seat`
    + ` · ${fmtNum(_finSeats)} assentos contratados`
    + ` → <strong style="color:var(--accent)">${brl(monthlyCents)}/mês</strong>`;
}

async function finSaveBilling() {
  const err = document.getElementById('finError');
  const ovRaw = document.getElementById('finOverride').value.trim();
  const seat_price_cents = ovRaw === '' ? null : Math.round(parseFloat(ovRaw) * 100);
  const discount_pct = parseFloat(document.getElementById('finDiscount').value || '0');
  try {
    await api('/v1/admin/tenant-billing?tenant=' + encodeURIComponent(_finTenant), {
      method: 'PUT', body: JSON.stringify({ seat_price_cents, discount_pct }),
    });
    toast('Faturamento atualizado.');
    finOpen(_finTenant);   // recarrega cálculo
  } catch (e) { err.textContent = 'Falha: ' + e.message; err.style.display = 'block'; }
}

function finRenderPayments(list) {
  const tb = document.getElementById('finPayBody');
  if (!tb) return;
  if (!list.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">Nenhum pagamento registrado.</td></tr>'; return; }
  tb.innerHTML = list.map(p => `<tr>
    <td class="mono">${escHtml(p.competencia)}</td>
    <td class="num">${brl(p.amount_cents)}</td>
    <td>${p.paid_at ? escHtml(p.paid_at) : '—'}</td>
    <td>${escHtml(p.method || '—')}${p.note ? `<div style="font-size:11px;color:var(--text-muted)">${escHtml(p.note)}</div>` : ''}</td>
    <td><button class="ghost" onclick="finDelPayment(${p.id})">excluir</button></td>
  </tr>`).join('');
}

async function finAddPayment() {
  const err = document.getElementById('finError');
  const comp = document.getElementById('finPayComp').value;   // 'AAAA-MM'
  const amt = Math.round(parseFloat(document.getElementById('finPayAmt').value || '0') * 100);
  if (!comp) { err.textContent = 'Informe a competência.'; err.style.display = 'block'; return; }
  if (!(amt >= 0)) { err.textContent = 'Valor inválido.'; err.style.display = 'block'; return; }
  try {
    await api('/v1/admin/payments?tenant=' + encodeURIComponent(_finTenant), {
      method: 'POST',
      body: JSON.stringify({
        competencia: comp, amount_cents: amt,
        paid_at: document.getElementById('finPayDate').value || null,
        method: document.getElementById('finPayMethod').value.trim() || null,
        note: document.getElementById('finPayNote').value.trim() || null,
      }),
    });
    ['finPayAmt', 'finPayMethod', 'finPayNote'].forEach(id => document.getElementById(id).value = '');
    toast('Pagamento registrado.');
    finOpen(_finTenant);
  } catch (e) { err.textContent = 'Falha: ' + e.message; err.style.display = 'block'; }
}

async function finDelPayment(id) {
  if (!confirm('Excluir este pagamento?')) return;
  try {
    await api('/v1/admin/payments/' + id + '?tenant=' + encodeURIComponent(_finTenant), { method: 'DELETE' });
    toast('Pagamento excluído.');
    finOpen(_finTenant);
  } catch (e) { toast('Falha: ' + e.message); }
}

function finClose() { document.getElementById('finModal').classList.add('hide'); }

async function loadAwsCosts() {
  const box = document.getElementById('awsCostSummary');
  if (!box) return;
  try {
    const d = await api('/v1/admin/aws-costs');
    if (!d.available) {
      box.innerHTML = `<span style="color:var(--text-muted);font-size:13px">Custos indisponíveis: ${escHtml(d.error || 'sem permissão')}. Confira a permissão ce:GetCostAndUsage (terraform apply).</span>`;
      return;
    }
    const cur = d.current_month || {};
    const usage = cur.usage || 0, net = cur.net || 0, savings = cur.savings || 0;
    const _fc = d.forecast_usage;
    let _html =
      `<span><strong>Custo de uso (mês):</strong> US$ ${usage.toFixed(2)}</span>` +
      (_fc != null ? `<span><strong>Projeção do mês:</strong> US$ ${_fc.toFixed(2)}</span>` : '') +
      `<span><strong>Faturado hoje:</strong> US$ ${net.toFixed(2)}</span>` +
      `<span><strong>Custo/usuário:</strong> ${d.cost_per_user != null ? ('US$ ' + d.cost_per_user.toFixed(2)) : '—'}</span>` +
      `<span><strong>Usuários:</strong> ${fmtNum(d.active_users || 0)}</span>`;
    if (d.free_tier && savings > 0.01) {
      _html += `<span style="color:#4ade80"><strong>Nível gratuito:</strong> −US$ ${savings.toFixed(2)}</span>`;
    }
    box.innerHTML = _html;
    const months = d.months || [];
    const totals = {};
    months.forEach(m => Object.entries(m.services || {}).forEach(([s, v]) => { totals[s] = (totals[s] || 0) + v; }));
    const top = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, 6).map(x => x[0]);
    const palette = ['#6c8cff', '#f472b6', '#34d399', '#a78bfa', '#f59e0b', '#22d3ee', '#94a3b8'];
    const labels = months.map(m => m.month);
    const datasets = top.map((s, i) => ({
      label: s, backgroundColor: palette[i % palette.length],
      data: months.map(m => m.services[s] || 0),
    }));
    datasets.push({
      label: 'Outros', backgroundColor: palette[6],
      data: months.map(m => {
        let o = 0; Object.entries(m.services || {}).forEach(([s, v]) => { if (!top.includes(s)) o += v; });
        return +o.toFixed(2);
      }),
    });
    if (state.charts.aws) state.charts.aws.destroy();
    state.charts.aws = new Chart(document.getElementById('chartAws').getContext('2d'), {
      type: 'bar',
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: '#8c97c4', boxWidth: 12, font: { size: 10 } } },
          tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: US$ ${(c.parsed.y || 0).toFixed(2)}` } },
        },
        scales: {
          x: { stacked: true, ticks: { color: '#8c97c4' }, grid: { display: false } },
          y: { stacked: true, beginAtZero: true, ticks: { color: '#8c97c4', callback: (v) => 'US$ ' + v }, grid: { color: 'rgba(140,151,196,.15)' } },
        },
      },
    });
  } catch (e) { box.innerHTML = `<span style="color:var(--danger)">Erro: ${escHtml(e.message)}</span>`; }
}

// ---------- Audit ----------
async function loadAudit() {
  try {
    const r = await api('/v1/admin/audit?limit=200');
    state.audit = r.entries || [];
    renderAudit();
  } catch (e) { toast('Erro: '+e.message); }
}
function renderAudit() {
  const q = (document.getElementById('auditFilter').value || '').toLowerCase();
  const rows = state.audit.filter(a =>
    !q || (a.actor||'').toLowerCase().includes(q)
       || (a.action||'').toLowerCase().includes(q)
       || (a.target||'').toLowerCase().includes(q)
       || (a.ip||'').toLowerCase().includes(q)
  );
  document.getElementById('auditCount').textContent = `${rows.length} registros`;
  document.getElementById('auditFeed').innerHTML = rows.length ? rows.map(a => {
    const action = a.action || '—';
    const tone   = action.startsWith('license') ? 'ok' : action.startsWith('tenant') ? 'warn' : 'muted';
    const meta   = [a.target, a.detail, a.ip].filter(Boolean).join(' · ');
    return `
    <div class="feed-row">
      <div class="when">${fmtDateTime(a.ts)}</div>
      <div class="what"><span class="badge ${tone}">${escHtml(action)}</span></div>
      <div class="who">${escHtml(a.actor || '—')}${meta ? `<small>${escHtml(meta)}</small>` : ''}</div>
    </div>`;
  }).join('') : '<div class="empty">Sem registros de auditoria.</div>';
}


// ---------- Helper de escape (recuperado: estava ausente) ----------
function escHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}


// ===========================================================================
// Logins dos clientes (gestão cross-tenant de dash_users)
// ===========================================================================
const _CL_ROLE = { admin_master: 'Admin Master', supervisor: 'Supervisor', lider: 'Líder', wp_admin: 'WorkPulse' };

function _clErr(msg) {
  const e = document.getElementById('clError');
  if (e) { e.textContent = msg || ''; e.style.display = msg ? 'block' : 'none'; }
}

async function loadLogins() {
  const tb = document.querySelector('#clTable tbody');
  if (!tb) return;
  await _clPopulateTenants();
  try {
    const data = await api('/v1/admin/users');
    state.logins = data.users || [];
    clRender();
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="6" class="empty">Erro: ${escHtml(e.message)}</td></tr>`;
  }
}

// Popula o dropdown de clientes (tenants) na aba Logins.
async function _clPopulateTenants() {
  const sel = document.getElementById('clTenant');
  if (!sel) return;
  if (!state.tenants || !state.tenants.length) {
    try { const r = await api('/v1/admin/tenants'); state.tenants = r.tenants || []; } catch (_) {}
  }
  const current = sel.value;
  const opts = ['<option value="">— selecione —</option>'].concat(
    [...(state.tenants || [])]
      .sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)))
      .map(t => `<option value="${escHtml(t.id)}">${escHtml(t.name || t.id)} (${escHtml(t.id)})</option>`)
  );
  sel.innerHTML = opts.join('');
  if (current) sel.value = current;
}

function clRender() {
  const tb = document.querySelector('#clTable tbody');
  if (!tb) return;
  const q = (document.getElementById('clFilter').value || '').toLowerCase();
  const rows = (state.logins || []).filter(u =>
    !q || (u.email||'').toLowerCase().includes(q) ||
    (u.tenant_id||'').toLowerCase().includes(q) || (u.role||'').toLowerCase().includes(q));
  const cnt = document.getElementById('clCount');
  if (cnt) cnt.textContent = `${rows.length} de ${(state.logins||[]).length}`;
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">Nenhum login.</td></tr>'; return; }
  tb.innerHTML = rows.map(u => {
    const dis = u.status !== 'active';
    const st  = dis ? '<span class="badge muted">bloqueado</span>' : '<span class="badge ok">ativo</span>';
    const tgl = dis
      ? `<button class="ghost" onclick="clSetStatus(${u.id},'active')">reativar</button>`
      : `<button class="ghost" onclick="clSetStatus(${u.id},'disabled')">bloquear</button>`;
    return `<tr>
      <td>${escHtml(u.tenant_id || '—')}</td>
      <td>${escHtml(u.display_name || '—')}</td>
      <td>${escHtml(u.email)}</td>
      <td>${_CL_ROLE[u.role] || u.role}</td>
      <td>${st}</td>
      <td style="white-space:nowrap">
        <button class="ghost" onclick="clReset(${u.id})">reenviar senha</button>
        ${tgl}
        <button class="ghost" onclick="clDelete(${u.id}, '${escHtml(u.email)}')">excluir</button>
      </td></tr>`;
  }).join('');
}

async function clCreate() {
  _clErr('');
  const tenant = document.getElementById('clTenant').value.trim();
  const name   = document.getElementById('clName').value.trim();
  const email  = document.getElementById('clEmail').value.trim();
  const role   = document.getElementById('clRole').value;
  if (!email) { _clErr('Informe o e-mail.'); return; }
  if (role !== 'wp_admin' && !tenant) { _clErr('Informe o Tenant ID para perfis de cliente.'); return; }
  try {
    const res = await api('/v1/admin/users', { method: 'POST', body: JSON.stringify({ tenant_id: tenant || null, email, display_name: name, role }) });
    document.getElementById('clName').value = '';
    document.getElementById('clEmail').value = '';
    _clShowLink(email, res && res.set_password_link);
    toast('Login criado. Link de senha enviado por e-mail.');
    loadLogins();
  } catch (e) { _clErr('Falha: ' + e.message); }
}

// Exibe o link de definição de senha para cópia/envio manual.
function _clShowLink(email, link) {
  const box = document.getElementById('clLinkBox');
  if (!box) return;
  if (!link) { box.style.display = 'none'; return; }
  const inp = document.getElementById('clLink');
  const forEl = document.getElementById('clLinkFor');
  if (forEl) forEl.textContent = email || '';
  if (inp) inp.value = link;
  box.style.display = 'block';
}

function clCopyLink() {
  const inp = document.getElementById('clLink');
  if (!inp || !inp.value) return;
  inp.select();
  navigator.clipboard ? navigator.clipboard.writeText(inp.value).then(() => toast('Link copiado.'), () => { document.execCommand('copy'); toast('Link copiado.'); })
                      : (document.execCommand('copy'), toast('Link copiado.'));
}

async function clSetStatus(id, status) {
  try { await api(`/v1/admin/users/${id}`, { method: 'PUT', body: JSON.stringify({ status }) }); toast('Atualizado.'); loadLogins(); }
  catch (e) { toast('Falha: ' + e.message); }
}
async function clReset(id) {
  try {
    const res = await api(`/v1/admin/users/${id}/reset-password`, { method: 'POST' });
    const u = (state.logins || []).find(x => x.id === id);
    _clShowLink(u ? u.email : '', res && res.reset_password_link);
    toast('Link de redefinição enviado.');
  } catch (e) { toast('Falha: ' + e.message); }
}
async function clDelete(id, email) {
  const hard = confirm('Excluir ' + email + '?\n\nOK = exclusão definitiva (hard-delete)\nCancelar = apenas desativar (soft-delete)');
  try {
    await api(`/v1/admin/users/${id}` + (hard ? '?hard=true' : ''), { method: 'DELETE' });
    toast(hard ? 'Login excluído definitivamente.' : 'Login desativado.');
    loadLogins();
  } catch (e) { toast('Falha: ' + e.message); }
}


// ===========================================================================
// Bootstrap — restaura sessão salva e valida o token (recuperado)
// ===========================================================================
(async function admBootstrap() {
  applyTheme(localStorage.getItem('wp_admin_theme') || 'dark');
  if (typeof _admInitMenu === 'function') _admInitMenu();

  const url   = localStorage.getItem('wp_admin_url');
  const token = localStorage.getItem('wp_admin_token');
  const urlEl = document.getElementById('loginUrl');
  if (url && urlEl) urlEl.value = url;

  if (!token || _admSessionExpired()) {
    if (token) {
      localStorage.removeItem('wp_admin_token');
      localStorage.removeItem('wp_admin_last_active');
    }
    state.token = '';
    document.getElementById('login').classList.remove('hide');
    return;
  }

  state.url = url || state.url;
  state.token = token;
  try {
    const r = await fetch(state.url + '/v1/admin/stats', { headers: { 'X-Admin-Token': token } });
    if (!r.ok) throw new Error('token inválido');
  } catch (_) {
    localStorage.removeItem('wp_admin_token');
    state.token = '';
    document.getElementById('login').classList.remove('hide');
    return;
  }

  document.getElementById('login').classList.add('hide');
  _admTouchSession(true);
  _admStartIdleWatch();
  loadAll();
})();


// ===========================================================================
// Edição de licença (modal)
// ===========================================================================
let _leLicId = null;
function licEdit(licId) {
  const l = (state.licenses || []).find(x => x.lic_id === licId);
  if (!l) { toast('Licença não encontrada'); return; }
  _leLicId = licId;
  document.getElementById('leLicId').textContent = licId;
  const t = (state.tenants || []).find(x => x.id === l.tenant_id);
  document.getElementById('leTenant').textContent = 'Empresa: ' + (t ? (t.name + ' (' + t.id + ')') : l.tenant_id);
  document.getElementById('lePlan').value  = l.plan || 'business';
  document.getElementById('leSeats').value = l.seats || 1;
  const exp = String(l.expires || '');
  document.getElementById('leExpires').value = /^\d{4}-\d{2}-\d{2}/.test(exp) ? exp.slice(0, 10) : '';
  document.getElementById('leError').style.display = 'none';
  document.getElementById('licEditModal').classList.remove('hide');
}
function leAddDays(n) {
  const inp = document.getElementById('leExpires');
  const base = inp.value ? new Date(inp.value + 'T12:00:00') : new Date();
  base.setDate(base.getDate() + n);
  inp.value = base.toISOString().slice(0, 10);
}
function leClose() { document.getElementById('licEditModal').classList.add('hide'); }
async function leSave() {
  const err = document.getElementById('leError');
  const show = (m) => { err.textContent = m; err.style.display = 'block'; };
  const plan = document.getElementById('lePlan').value;
  const seats = parseInt(document.getElementById('leSeats').value, 10);
  const expires = document.getElementById('leExpires').value;
  if (!seats || seats < 1) { show('Quantidade de seats inválida.'); return; }
  try {
    const res = await api(`/v1/admin/licenses/${encodeURIComponent(_leLicId)}`, {
      method: 'PATCH', body: JSON.stringify({ plan, seats, expires: expires || null }),
    });
    toast('Licença atualizada.');
    loadTenantsLicenses();
    leClose();
  } catch (e) { show('Falha: ' + e.message); }
}


async function loadTenantCosts() {
  const tb = document.querySelector('#tcTable tbody');
  if (!tb) return;
  const driver = (document.getElementById('tcDriver') || {}).value || 'users';
  try {
    const d = await api('/v1/admin/tenant-costs?driver=' + encodeURIComponent(driver));
    const sumEl = document.getElementById('tcSummary');
    if (!d.available) {
      if (sumEl) sumEl.textContent = 'Custos AWS indisponíveis — o rateio precisa do custo de uso.';
      tb.innerHTML = '<tr><td colspan="8" class="empty">Sem base de custo.</td></tr>';
      return;
    }
    if (sumEl) sumEl.innerHTML = `Base: <strong>US$ ${(d.usage_total || 0).toFixed(2)}</strong> de uso no mês (${d.month || '—'}), rateado por ${d.driver === 'devices' ? 'agentes' : 'usuários'}.`;
    const rows = d.tenants || [];
    if (!rows.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">Nenhum cliente.</td></tr>'; return; }
    tb.innerHTML = rows.map(t => `<tr>
      <td><strong>${escHtml(t.name || t.tenant_id)}</strong><div class="mono" style="font-size:11px;color:var(--text-muted)">${escHtml(t.tenant_id)}</div></td>
      <td>${planBadge(t.plan)}</td>
      <td class="num">${fmtNum(t.users)}</td>
      <td class="num">${fmtNum(t.devices)}</td>
      <td class="num">${fmtNum(t.events || 0)}</td>
      <td class="num"><strong>US$ ${(t.cost || 0).toFixed(2)}</strong></td>
      <td class="num">${t.cost_per_user  != null ? ('US$ ' + t.cost_per_user.toFixed(2))  : '—'}</td>
      <td class="num">${t.cost_per_agent != null ? ('US$ ' + t.cost_per_agent.toFixed(2)) : '—'}</td>
    </tr>`).join('');
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="8" class="empty">Erro: ${escHtml(e.message)}</td></tr>`;
  }
}


async function runSim() {
  const tb = document.querySelector('#simTable tbody');
  if (!tb) return;
  const n = parseInt((document.getElementById('simUsers') || {}).value, 10);
  try {
    const d = await api('/v1/admin/cost-simulate' + (n > 0 ? ('?users=' + n) : ''));
    const sumEl = document.getElementById('simSummary');
    if (!d.available) {
      if (sumEl) sumEl.textContent = 'Custos AWS indisponíveis.';
      tb.innerHTML = '<tr><td colspan="3" class="empty">Sem base de custo.</td></tr>';
      return;
    }
    if (sumEl) sumEl.innerHTML = `<strong>${fmtNum(d.users)}</strong> usuários → total <strong>US$ ${(d.total || 0).toFixed(2)}/mês</strong> · custo/usuário <strong>US$ ${d.cost_per_user != null ? d.cost_per_user.toFixed(2) : '—'}</strong>`;
    const rows = d.items || [];
    tb.innerHTML = rows.map(it => `<tr>
      <td>${escHtml(it.service)}</td>
      <td>${it.fixed ? '<span class="badge muted">fixo</span>' : '<span class="badge ok">variável</span>'}</td>
      <td class="num">US$ ${(it.projected || 0).toFixed(2)}</td>
    </tr>`).join('') +
      `<tr style="font-weight:600;border-top:2px solid var(--border)"><td>Total</td><td></td><td class="num">US$ ${(d.total || 0).toFixed(2)}</td></tr>`;
    const inp = document.getElementById('simUsers');
    if (inp && !inp.value) inp.value = d.users;
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="3" class="empty">Erro: ${escHtml(e.message)}</td></tr>`;
  }
}
