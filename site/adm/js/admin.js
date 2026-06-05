
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
    const loaders = { overview: loadOverview, tenants: loadTenants, licenses: loadLicenses, system: loadSystem, audit: loadAudit };
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
function doLogin() {
  _admClearLoginError();
  state.url   = document.getElementById('loginUrl').value.trim().replace(/\/$/, '');
  state.token = document.getElementById('loginToken').value.trim();
  if (!state.url || !state.token) { _admShowLoginError('Preencha o endpoint e o token.'); return; }
  localStorage.setItem('wp_admin_url', state.url);
  localStorage.setItem('wp_admin_token', state.token);
  _admTouchSession(true);
  document.getElementById('login').classList.add('hide');
  _admStartIdleWatch();
  loadAll();
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
    ['overview','tenants','licenses','system','audit'].forEach(t =>
      document.getElementById('tab-'+t).classList.add('hide'));
    document.getElementById('tab-'+btn.dataset.tab).classList.remove('hide');
    if (btn.dataset.tab === 'overview') loadOverview();
    if (btn.dataset.tab === 'tenants')  loadTenants();
    if (btn.dataset.tab === 'licenses') loadLicenses();
    if (btn.dataset.tab === 'system')   loadSystem();
    if (btn.dataset.tab === 'audit')    loadAudit();
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
async function loadTenants() {
  try {
    const r = await api('/v1/admin/tenants');
    state.tenants = r.tenants || [];
    renderTenants();
  } catch (e) { toast('Erro: '+e.message); }
}
function renderTenants() {
  const q = (document.getElementById('tenantFilter').value || '').toLowerCase();
  const plan = document.getElementById('tenantPlanFilter').value;
  const rows = state.tenants.filter(t =>
    (!q || t.id.toLowerCase().includes(q) || (t.name||'').toLowerCase().includes(q)) &&
    (!plan || t.plan === plan)
  );
  document.getElementById('tenantCount').textContent = `${rows.length} de ${state.tenants.length}`;
  document.querySelector('#tenantsTable tbody').innerHTML = rows.length ? rows.map(t => `
    <tr>
      <td class="mono">${t.id}</td>
      <td><strong>${t.name}</strong></td>
      <td>${planBadge(t.plan)}</td>
      <td><span class="badge ${t.status === 'active' ? 'ok' : 'muted'}">${t.status}</span></td>
      <td class="num">${fmtNum(t.users)}</td>
      <td class="num">${fmtNum(t.devices)}</td>
      <td class="num">${fmtNum(t.active_licenses)}</td>
      <td>${fmtDate(t.created_at)}</td>
      <td>${relTime(t.last_activity)}</td>
      <td class="num">
        <span style="color:var(--text-dim);font-size:12px">${t.retention_days != null ? t.retention_days + 'd' : '90d ⚙'}</span>
        <button class="ghost" style="margin-left:4px;font-size:11px;padding:2px 6px" onclick="setRetention('${t.id}', ${t.retention_days || 90})">✎</button>
      </td>
      <td><button class="ghost" onclick="genTenantAccess('${t.id}')">gerar acesso</button></td>
    </tr>`).join('') : `<tr><td colspan="11" class="empty">Nenhum tenant encontrado.</td></tr>`;
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
    loadTenants();
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
    loadTenants();
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

// ---------- Licenses ----------
async function loadLicenses() {
  try {
    const r = await api('/v1/admin/licenses');
    state.licenses = r.licenses || [];
    renderLicenses();
  } catch (e) { toast('Erro: '+e.message); }
}
function renderLicenses() {
  const q = (document.getElementById('licTenantFilter').value || '').toLowerCase();
  const status = document.getElementById('licStatusFilter').value;

  const all = state.licenses;
  const filtered = all.filter(l => {
    if (q && !((l.tenant_id||'').toLowerCase().includes(q) || (l.tenant_name||'').toLowerCase().includes(q))) return false;
    if (status === 'active'   && (l.revoked || l.expired)) return false;
    if (status === 'revoked'  && !l.revoked) return false;
    if (status === 'expired'  && !l.expired) return false;
    if (status === 'expiring' && !(l.days_remaining !== null && l.days_remaining <= 30 && !l.expired && !l.revoked)) return false;
    return true;
  });

  document.getElementById('licCount').textContent = `${filtered.length} de ${all.length}`;

  // KPIs
  const active   = all.filter(l => !l.revoked && !l.expired).length;
  const expSoon  = all.filter(l => l.days_remaining !== null && l.days_remaining <= 30 && !l.expired && !l.revoked).length;
  const dead     = all.filter(l => l.revoked || l.expired).length;
  document.getElementById('licTotal').textContent  = fmtNum(all.length);
  document.getElementById('licActive').textContent = fmtNum(active);
  document.getElementById('licExpSoon').textContent = fmtNum(expSoon);
  document.getElementById('licDead').textContent   = fmtNum(dead);

  document.querySelector('#licensesTable tbody').innerHTML = filtered.length ? filtered.map(l => {
    const statusBadge = l.revoked
      ? '<span class="badge danger">revogada</span>'
      : l.expired
      ? '<span class="badge muted">expirada</span>'
      : l.days_remaining !== null && l.days_remaining <= 30
      ? `<span class="badge warn">expira ${l.days_remaining}d</span>`
      : '<span class="badge ok">ativa</span>';
    return `<tr>
      <td class="mono">${l.lic_id}</td>
      <td><strong>${l.tenant_name || l.tenant_id}</strong><br><span class="mono" style="color:var(--text-muted);font-size:11px">${l.tenant_id}</span></td>
      <td>${planBadge(l.plan)}</td>
      <td class="num">${fmtNum(l.seats)}</td>
      <td>${fmtDate(l.issued)}</td>
      <td>${fmtDate(l.expires)}</td>
      <td class="num mono">${l.days_remaining !== null ? l.days_remaining : '—'}</td>
      <td>${statusBadge}</td>
      <td>${l.revoked ? '' : `<button class="ghost" onclick="revokeLic('${l.lic_id}')">revogar</button>`}</td>
    </tr>`;
  }).join('') : `<tr><td colspan="9" class="empty">Nenhuma licença com esses filtros.</td></tr>`;
}

async function revokeLic(licId) {
  if (!confirm(`Revogar licença ${licId}? Esta ação não pode ser desfeita.`)) return;
  try {
    await api('/v1/license/revoke?lic_id=' + encodeURIComponent(licId), { method: 'POST' });
    toast('Licença revogada: ' + licId);
    loadLicenses();
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
  } catch (e) { toast('Erro: '+e.message); }
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
      <div class="what"><span class="badge ${tone}">${action}</span></div>
      <div class="who">${a.actor || '—'}${meta ? `<small>${meta}</small>` : ''}</div>
    </div>`;
  }).join('') : `<div class="empty">Nenhum registro encontrado.</div>`;
}

// =====================================================================
// Bootstrap
// =====================================================================
function loadAll() {
  loadOverview();   // default tab; other tabs load on click
}

(function() {
  // Aplica o tema salvo (padrão: escuro) e inicializa o menu do operador
  applyTheme(localStorage.getItem('wp_admin_theme') || 'dark');
  _admInitMenu();

  const token = localStorage.getItem('wp_admin_token');
  const url   = localStorage.getItem('wp_admin_url');

  // Prefill de conveniência do endpoint
  if (url) document.getElementById('loginUrl').value = url;

  if (token && url && !_admSessionExpired()) {
    document.getElementById('loginToken').value = token;
    state.token = token;
    state.url   = url;
    document.getElementById('login').classList.add('hide');
    _admTouchSession(true);   // renova o carimbo ao retomar
    _admStartIdleWatch();
    loadAll();
  } else if (token) {
    // Havia sessão salva, mas expirou por inatividade → exige novo login
    localStorage.removeItem('wp_admin_token');
    localStorage.removeItem('wp_admin_last_active');
    state.token = '';
  }
})();
