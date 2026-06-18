// =====================================================================
// wp-settings.js — Aba "Configurações"
// Inclui: regras de produtividade, apps detectados, departamentos,
//         colaboradores e toggle de tema.
// =====================================================================

let _allRules     = [];   // todas as regras (cache pós-load)
let _allApps      = [];   // apps não-browser (cache)
let _allSites     = [];   // sites detectados (cache)
let _appsFiltered = [];   // resultado do filtro de apps
let _sitesFiltered= [];   // resultado do filtro de sites
let _depts        = [];   // [{id, name, user_count, users:[...]}]

// Estado de paginação (padrão: 10 por página; a pessoa pode aumentar no seletor)
let _rulesPage   = 0; let _RULES_PAGE_SIZE   = 10;
let _appsPage    = 0; let _APPS_PAGE_SIZE    = 10;
let _sitesPage   = 0; let _SITES_PAGE_SIZE   = 10;
let _collabsPage = 0; let _COLLABS_PAGE_SIZE = 10;

/* ─────────────────────────────────────────────────────────────────
   Helper genérico de paginação (reutilizado nas 3 seções)
   ───────────────────────────────────────────────────────────────── */
function _renderCfgPagination(containerId, labelId, page, total, pageSize, goToFn, itemLabel) {
  const container  = document.getElementById(containerId);
  const labelEl    = document.getElementById(labelId);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const hasPrev    = page > 0;
  const hasNext    = page < totalPages - 1;
  const sizeFn     = goToFn.replace('GoTo', 'SetSize');  // ex.: _rulesGoTo -> _rulesSetSize

  if (labelEl) {
    labelEl.textContent = total > 0 ? `— ${total.toLocaleString('pt-BR')} ${itemLabel}` : '';
  }

  if (!container) return;

  // Cabe tudo no menor tamanho (10): sem paginação nem seletor.
  if (total <= 10) { container.innerHTML = ''; return; }

  // Seletor "por página" — sempre visível quando há mais de 10 itens,
  // mesmo numa única página (permite voltar a um tamanho menor).
  const sizeSel =
    `<select class="cfg-page-size" title="Itens por página" onchange="${sizeFn}(this.value)">` +
    [10, 25, 50, 100].map(n => `<option value="${n}"${n === pageSize ? ' selected' : ''}>${n}/página</option>`).join('') +
    `</select>`;

  // Navegação só quando há mais de uma página.
  let nav = '';
  if (hasPrev || hasNext) {
    const cur      = page + 1;
    const winStart = Math.max(1, cur - 2);
    const winEnd   = Math.min(totalPages, cur + 2);
    const btns     = [];
    if (winStart > 1) {
      btns.push(`<button class="ev-page-btn ghost" onclick="${goToFn}(0)">1</button>`);
      if (winStart > 2) btns.push(`<span class="ev-page-ellipsis">…</span>`);
    }
    for (let p = winStart; p <= winEnd; p++) {
      const active = p === cur ? ' ev-page-active' : '';
      btns.push(`<button class="ev-page-btn ghost${active}" onclick="${goToFn}(${p - 1})">${p}</button>`);
    }
    if (winEnd < totalPages) {
      if (winEnd < totalPages - 1) btns.push(`<span class="ev-page-ellipsis">…</span>`);
      btns.push(`<button class="ev-page-btn ghost" onclick="${goToFn}(${totalPages - 1})">${totalPages}</button>`);
    }
    nav =
      `<button class="ghost ev-page-nav" ${hasPrev ? '' : 'disabled'} onclick="${goToFn}(${page - 1})">← Anterior</button>` +
      `<div class="ev-page-numbers">${btns.join('')}</div>` +
      `<button class="ghost ev-page-nav" ${hasNext ? '' : 'disabled'} onclick="${goToFn}(${page + 1})">Próxima →</button>`;
  }

  container.innerHTML = `<div class="ev-pagination">${sizeSel}${nav}</div>`;
}

/* Seletores de "itens por página" (chamados pelo <select> da paginação). */
function _rulesSetSize(v)   { _RULES_PAGE_SIZE   = Math.max(1, parseInt(v, 10) || 10); _rulesPage   = 0; _renderRulesPage(); }
function _appsSetSize(v)    { _APPS_PAGE_SIZE    = Math.max(1, parseInt(v, 10) || 10); _appsPage    = 0; _renderAppsPage(); }
function _sitesSetSize(v)   { _SITES_PAGE_SIZE   = Math.max(1, parseInt(v, 10) || 10); _sitesPage   = 0; _renderSitesPage(); }
function _collabsSetSize(v) { _COLLABS_PAGE_SIZE = Math.max(1, parseInt(v, 10) || 10); _collabsPage = 0; _renderCollabsPage(); }

/* -------------------- LOAD CONFIG -------------------- */
async function loadConfig() {
  await Promise.all([loadRules(), loadAppsForConfig(), loadSitesForConfig(), loadCollabs(), loadDepts(), loadReports()]);
  const btn = document.getElementById('themeToggleBtn');
  if (btn) {
    const isLight = document.body.classList.contains('theme-light');
    btn.textContent = isLight ? '🌙 Tema Escuro' : '☀️ Tema Claro';
  }
}

/* ──────────────────────────────────────────────────────────────────
   REGRAS
   ────────────────────────────────────────────────────────────────── */
async function loadRules() {
  try {
    const data = await api('/v1/productivity/rules?tenant=' + encodeURIComponent(state.tenant));
    _allRules  = data.rules || [];
    _rulesPage = 0;
    _renderRulesPage();
  } catch (e) { toast('Erro ao carregar regras: ' + e.message); }
}

function _renderRulesPage() {
  const tbody      = document.querySelector('#rulesTable tbody');
  const fieldLabel = { title: 'Título', app: 'Aplicativo' };
  const typeLabel  = { contains: 'Contém', exact: 'Exato', startswith: 'Começa com' };
  const total      = _allRules.length;

  if (!total) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">Nenhuma regra cadastrada ainda.<br><small>Use o formulário acima para criar a primeira.</small></td></tr>';
    _renderCfgPagination('rulesPagination', 'rulesCountLabel', 0, 0, _RULES_PAGE_SIZE, '_rulesGoTo', 'regras');
    return;
  }

  const slice = _allRules.slice(_rulesPage * _RULES_PAGE_SIZE, (_rulesPage + 1) * _RULES_PAGE_SIZE);
  tbody.innerHTML = slice.map(r =>
    `<tr>
      <td><strong>${r.label}</strong></td>
      <td><span class="pill">${fieldLabel[r.match_field] || r.match_field}</span></td>
      <td><code style="color:var(--text-dim)">${r.pattern}</code></td>
      <td style="color:var(--text-dim);font-size:12px">${typeLabel[r.match_type] || r.match_type}</td>
      <td>${CAT_PILLS[r.category] || r.category}</td>
      <td><button class="del-btn" onclick="deleteRule(${r.id})">Excluir</button></td>
    </tr>`
  ).join('');

  _renderCfgPagination('rulesPagination', 'rulesCountLabel', _rulesPage, total, _RULES_PAGE_SIZE, '_rulesGoTo', 'regras');
}

function _rulesGoTo(page) {
  _rulesPage = page;
  _renderRulesPage();
  document.getElementById('rulesTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderRulesTable(rules) {
  // Mantido para compatibilidade — atualiza o cache e re-renderiza
  _allRules  = rules;
  _rulesPage = 0;
  _renderRulesPage();
}

async function saveRule() {
  const label       = document.getElementById('ruleLabel').value.trim();
  const pattern     = document.getElementById('rulePattern').value.trim();
  const match_field = document.getElementById('ruleField').value;
  const match_type  = document.getElementById('ruleType').value;
  const category    = document.getElementById('ruleCategory').value;
  if (!label || !pattern) { toast('Preencha o rótulo e o padrão.'); return; }
  try {
    await apiFetch('POST', '/v1/productivity/rules?tenant=' + encodeURIComponent(state.tenant),
      { label, pattern, match_field, match_type, category });
    toast('Regra salva com sucesso!');
    document.getElementById('ruleLabel').value   = '';
    document.getElementById('rulePattern').value = '';
    await loadRules();
  } catch (e) { toast('Erro ao salvar regra: ' + e.message); }
}

async function deleteRule(id) {
  if (!confirm('Excluir esta regra?')) return;
  try {
    await apiFetch('DELETE', '/v1/productivity/rules/' + id + '?tenant=' + encodeURIComponent(state.tenant));
    toast('Regra excluída.');
    await loadRules();
  } catch (e) { toast('Erro ao excluir regra: ' + e.message); }
}

function quickRule(pattern, field) {
  switchTab('categorizacao');
  document.getElementById('ruleLabel').value   = pattern;
  document.getElementById('rulePattern').value = pattern;
  if (field) document.getElementById('ruleField').value = field;
  document.getElementById('ruleLabel').focus();
  document.getElementById('ruleLabel').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* ──────────────────────────────────────────────────────────────────
   APLICATIVOS DETECTADOS
   ────────────────────────────────────────────────────────────────── */
async function loadAppsForConfig() {
  try {
    const data = await api('/v1/productivity/apps?tenant=' + encodeURIComponent(state.tenant) + '&days=30');
    _allApps      = (data.apps || []).filter(a => !a.is_browser);
    _appsFiltered = _allApps;
    _appsPage     = 0;
    _renderAppsPage();
  } catch (e) { toast('Erro ao carregar apps: ' + e.message); }
}

function _renderAppsPage() {
  const el    = document.getElementById('appsList');
  const total = _appsFiltered.length;
  const catDot = { productive: '#4ade80', unproductive: '#f87171', neutral: '#94a3b8', uncategorized: '#fbbf24' };

  if (!total) {
    el.innerHTML = '<p style="color:var(--text-muted);font-size:13px">Nenhum aplicativo detectado no período.</p>';
    _renderCfgPagination('appsPagination', 'appsCountLabel', 0, 0, _APPS_PAGE_SIZE, '_appsGoTo', 'apps');
    return;
  }

  const slice = _appsFiltered.slice(_appsPage * _APPS_PAGE_SIZE, (_appsPage + 1) * _APPS_PAGE_SIZE);
  el.innerHTML = '<div class="chip-grid">' + slice.map(a => {
    const dot      = catDot[a.category] || catDot.uncategorized;
    const safeName = (a.app || '').replace(/'/g, "\\'");
    return `<span class="chip"
      onclick="quickRule('${safeName}', 'app')"
      title="${a.app} — ${fmtSec(a.total_sec)} · ${a.user_count} usuário(s)">
      <span class="chip-dot" style="background:${dot}"></span>
      <span class="chip-label">${a.app}</span>
      <span class="chip-time">${fmtSec(a.total_sec)}</span>
    </span>`;
  }).join('') + '</div>';

  _renderCfgPagination('appsPagination', 'appsCountLabel', _appsPage, total, _APPS_PAGE_SIZE, '_appsGoTo', 'apps');
}

function _appsGoTo(page) {
  _appsPage = page;
  _renderAppsPage();
  document.getElementById('appsList')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function filterApps() {
  const q       = (document.getElementById('appsFilter').value || '').toLowerCase();
  _appsFiltered = q ? _allApps.filter(a => (a.app || '').toLowerCase().includes(q)) : _allApps;
  _appsPage     = 0;
  _renderAppsPage();
}

// Mantido para compatibilidade
function renderAppsList(apps) {
  _appsFiltered = apps;
  _appsPage     = 0;
  _renderAppsPage();
}

/* ──────────────────────────────────────────────────────────────────
   SITES DETECTADOS
   ────────────────────────────────────────────────────────────────── */
async function loadSitesForConfig() {
  try {
    const data = await api('/v1/productivity/internet?tenant=' + encodeURIComponent(state.tenant) + '&days=30');
    _allSites      = data.sites || [];
    _sitesFiltered = _allSites;
    _sitesPage     = 0;
    _renderSitesPage();
  } catch (e) { toast('Erro ao carregar sites: ' + e.message); }
}

function _renderSitesPage() {
  const el    = document.getElementById('sitesList');
  if (!el) return;
  const total = _sitesFiltered.length;
  const catDot = { productive: '#4ade80', unproductive: '#f87171', neutral: '#94a3b8', uncategorized: '#fbbf24' };

  if (!total) {
    el.innerHTML = '<p style="color:var(--text-muted);font-size:13px">Nenhum site detectado no período.</p>';
    _renderCfgPagination('sitesPagination', 'sitesCountLabel', 0, 0, _SITES_PAGE_SIZE, '_sitesGoTo', 'sites');
    return;
  }

  const slice = _sitesFiltered.slice(_sitesPage * _SITES_PAGE_SIZE, (_sitesPage + 1) * _SITES_PAGE_SIZE);
  el.innerHTML = '<div class="chip-grid">' + slice.map(s => {
    const dot      = catDot[s.category] || catDot.uncategorized;
    const safeName = (s.site_name || '').replace(/'/g, "\\'");
    return `<span class="chip"
      onclick="quickRule('${safeName}', 'title')"
      title="${s.site_name} — ${fmtSec(s.total_sec)} · ${s.user_count} usuário(s)">
      <span class="chip-dot" style="background:${dot}"></span>
      <span class="chip-label">${s.site_name}</span>
      <span class="chip-time">${fmtSec(s.total_sec)}</span>
    </span>`;
  }).join('') + '</div>';

  _renderCfgPagination('sitesPagination', 'sitesCountLabel', _sitesPage, total, _SITES_PAGE_SIZE, '_sitesGoTo', 'sites');
}

function _sitesGoTo(page) {
  _sitesPage = page;
  _renderSitesPage();
  document.getElementById('sitesList')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function filterSites() {
  const q        = (document.getElementById('sitesFilter').value || '').toLowerCase();
  _sitesFiltered = q ? _allSites.filter(s => (s.site_name || '').toLowerCase().includes(q)) : _allSites;
  _sitesPage     = 0;
  _renderSitesPage();
}

// Mantido para compatibilidade
function renderSitesList(sites) {
  _sitesFiltered = sites;
  _sitesPage     = 0;
  _renderSitesPage();
}

/* ──────────────────────────────────────────────────────────────────
   DEPARTAMENTOS
   ────────────────────────────────────────────────────────────────── */
function _updateDeptDatalist() {
  const dl = document.getElementById('deptDatalist');
  if (!dl) return;
  dl.innerHTML = _depts.map(d => `<option value="${d.name}">`).join('');
}

async function loadDepts() {
  try {
    const data = await api('/v1/departments?tenant=' + encodeURIComponent(state.tenant));
    _depts = data.departments || [];
    _updateDeptDatalist();
    renderDeptsSection();
  } catch (e) { toast('Erro ao carregar departamentos: ' + e.message); }
}

function renderDeptsSection() {
  const el = document.getElementById('deptsList');
  if (!el) return;
  if (!_depts.length) {
    el.innerHTML = '<p style="color:var(--text-muted);font-size:13px">Nenhum departamento criado ainda. Use o formulário acima para começar.</p>';
    return;
  }
  const assignedIds = new Set(_depts.flatMap(d => (d.users || []).map(u => u.user_id)));
  const unassigned  = state.collabs.filter(u => !assignedIds.has(u.user_id));

  el.innerHTML = _depts.map(dept => {
    const safeName = dept.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    const userChips = (dept.users || []).map(u => {
      const label   = u.display_name || u.login_user || cleanUserId(u.user_id);
      const safeUid = (u.user_id || '').replace(/'/g, "\\'");
      return `<span class="dept-user-chip">
        ${avatar(label)}
        <span>${label}</span>
        <button title="Remover do departamento" onclick="removeUserFromDept('${safeUid}','${safeName}')">×</button>
      </span>`;
    }).join('');

    const unassignedOpts = unassigned.map(u => {
      const label   = u.display_name || cleanUserId(u.user_id);
      const safeUid = (u.user_id || '').replace(/"/g, '&quot;');
      return `<option value="${safeUid}">${label}</option>`;
    }).join('');

    return `<div class="dept-mgmt-card">
      <div class="dept-header">
        <div class="dept-name">
          ${dept.name}
          <span class="dept-count">${dept.user_count} pessoa${dept.user_count !== 1 ? 's' : ''}</span>
        </div>
        <div class="dept-actions">
          <button class="del-btn" onclick="deleteDept('${safeName}')">Excluir</button>
        </div>
      </div>
      <div class="dept-users-list">${userChips || '<span style="color:var(--text-muted);font-size:12px">Nenhum colaborador neste departamento.</span>'}</div>
      <div class="dept-add-user">
        <select id="addUserSel_${dept.id}">
          <option value="">— adicionar colaborador —</option>
          ${unassignedOpts}
        </select>
        <button class="ghost" style="font-size:12px;padding:5px 12px" onclick="assignUserToDept(${dept.id},'${safeName}')">Adicionar</button>
      </div>
    </div>`;
  }).join('');
}

async function createDept() {
  const name = (document.getElementById('newDeptName').value || '').trim();
  if (!name) { toast('Digite o nome do departamento.'); return; }
  try {
    await apiFetch('POST', '/v1/departments?tenant=' + encodeURIComponent(state.tenant), { name });
    document.getElementById('newDeptName').value = '';
    toast(`Departamento "${name}" criado!`);
    await loadDepts();
  } catch (e) { toast('Erro: ' + e.message); }
}

async function deleteDept(name) {
  if (!confirm(`Excluir o departamento "${name}"?\nOs colaboradores perderão o vínculo, mas não serão excluídos.`)) return;
  try {
    await apiFetch('DELETE', '/v1/departments/' + encodeURIComponent(name) + '?tenant=' + encodeURIComponent(state.tenant));
    toast('Departamento excluído.');
    await Promise.all([loadDepts(), loadCollabs()]);
  } catch (e) { toast('Erro: ' + e.message); }
}

async function assignUserToDept(deptId, deptName) {
  const sel = document.getElementById('addUserSel_' + deptId);
  const uid = sel ? sel.value : '';
  if (!uid) { toast('Selecione um colaborador.'); return; }
  try {
    await apiFetch('PATCH', '/v1/users/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant),
      { department: deptName });
    toast('Colaborador adicionado a ' + deptName + '.');
    await Promise.all([loadDepts(), loadCollabs()]);
  } catch (e) { toast('Erro: ' + e.message); }
}

async function removeUserFromDept(uid, deptName) {
  try {
    await apiFetch('PATCH', '/v1/users/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant),
      { department: '' });
    toast('Colaborador removido de ' + deptName + '.');
    await Promise.all([loadDepts(), loadCollabs()]);
  } catch (e) { toast('Erro: ' + e.message); }
}

/* ──────────────────────────────────────────────────────────────────
   COLABORADORES
   ────────────────────────────────────────────────────────────────── */
async function loadCollabs() {
  try {
    const data = await api('/v1/users?tenant=' + encodeURIComponent(state.tenant));
    state.collabs = data.users || [];
    _collabsPage  = 0;
    _renderCollabsPage();
  } catch (e) { toast('Erro ao carregar colaboradores: ' + e.message); }
}

// Mantido para compatibilidade — atualiza o cache e re-renderiza paginado
function renderCollabsTable(users) {
  if (users) state.collabs = users;
  _collabsPage = 0;
  _renderCollabsPage();
}

function _renderCollabsPage() {
  const tb = document.querySelector('#collabsTable tbody');
  if (!tb) return;
  const users = state.collabs || [];
  const total = users.length;

  if (!total) {
    tb.innerHTML = '<tr><td colspan="7" class="empty-state">Nenhum colaborador encontrado.</td></tr>';
    _renderCfgPagination('collabsPagination', 'collabsCountLabel', 0, 0, _COLLABS_PAGE_SIZE, '_collabsGoTo', 'colaboradores');
    return;
  }

  const isMaster = state.role === 'admin_master' || state.role === 'wp_admin';
  const slice = users.slice(_collabsPage * _COLLABS_PAGE_SIZE, (_collabsPage + 1) * _COLLABS_PAGE_SIZE);
  tb.innerHTML = slice.map(u => {
    const uid      = u.user_id || '';
    const safeUid  = uid.replace(/'/g, "\\'");
    const name     = u.display_name || cleanUserId(uid);
    const safeName = (name || '').replace(/'/g, "\\'");
    const danger = isMaster ? `
      <button class="ghost" title="Apaga as coletas, mantém o cadastro" onclick="collabPurge('${safeUid}','${safeName}')">🧹 Limpar coletas</button>
      <button class="btn-danger-outline" onclick="collabDelete('${safeUid}','${safeName}')">🗑 Excluir</button>` : '';
    return `<tr>
      <td><code style="color:var(--text-dim);font-size:11px">${cleanUserId(uid)}</code></td>
      <td>${u.display_name || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${u.email        || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${u.department   || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${u.role         || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td style="color:var(--text-dim);font-size:12px">${relTime(u.last_seen)}</td>
      <td style="white-space:nowrap;display:flex;gap:6px;flex-wrap:wrap"><button class="cat-btn" onclick="editUser('${safeUid}')">✏ Editar</button>${danger}</td>
    </tr>`;
  }).join('');

  _renderCfgPagination('collabsPagination', 'collabsCountLabel', _collabsPage, total, _COLLABS_PAGE_SIZE, '_collabsGoTo', 'colaboradores');
}

function _collabsGoTo(page) {
  _collabsPage = page;
  _renderCollabsPage();
  document.getElementById('collabsTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function editUser(uid) {
  if (typeof switchTab === 'function') {
    const current = document.querySelector('nav.tabs > button.active, .nav-dd-trigger.active');
    const tab = current?.dataset?.tab;
    if (tab !== 'colaboradores') switchTab('colaboradores');
  }
  const user = state.collabs.find(u => u.user_id === uid)
    || state.people.find(u => u.user_id === uid)
    || { user_id: uid };

  document.getElementById('collabUserId').value = uid;
  document.getElementById('collabDevice').value  = cleanUserId(uid);
  document.getElementById('collabName').value    = user.display_name || '';
  document.getElementById('collabEmail').value   = user.email || '';
  document.getElementById('collabDept').value    = user.department || '';
  document.getElementById('collabRole').value    = user.role || '';
  _updateDeptDatalist();

  const form = document.getElementById('collabForm');
  form.style.display = 'grid';
  setTimeout(() => {
    document.getElementById('collabsSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    document.getElementById('collabName').focus();
  }, 120);
}

async function saveUser() {
  const uid  = document.getElementById('collabUserId').value;
  const body = {
    display_name: document.getElementById('collabName').value.trim()  || null,
    email:        document.getElementById('collabEmail').value.trim() || null,
    department:   document.getElementById('collabDept').value.trim()  || null,
    role:         document.getElementById('collabRole').value.trim()  || null,
  };
  Object.keys(body).forEach(k => { if (!body[k]) delete body[k]; });
  if (!Object.keys(body).length) { toast('Nada para salvar.'); return; }
  try {
    // apiFetch envia o cookie de sessão (credentials:'include') — necessário no
    // login e-mail/senha. O fetch cru anterior não mandava o cookie → 403.
    await apiFetch('PATCH',
      '/v1/users/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant),
      body);
    toast('Colaborador atualizado!');
    document.getElementById('collabForm').style.display = 'none';
    await loadCollabs();
  } catch (e) { toast('Erro ao salvar: ' + e.message); }
}

// Alias — chamado pela aba Internet para pré-preencher regra de site
function prefillRule(siteName) { quickRule(siteName, "site"); }


// ── Exclusão de coletas / colaborador + bloqueio (somente Admin Master) ──────
async function collabPurge(uid, name) {
  if (!confirm(`Limpar TODAS as coletas de "${name}"?\n\nOs dados coletados são apagados; o colaborador continua cadastrado e segue coletando normalmente.`)) return;
  try {
    await apiFetch('POST', '/v1/users/' + encodeURIComponent(uid) + '/purge-events?tenant=' + encodeURIComponent(state.tenant));
    toast('Coletas apagadas.');
    await loadCollabs();
  } catch (e) { toast('Falha ao limpar: ' + e.message); }
}

async function collabDelete(uid, name) {
  const typed = prompt(`Excluir o colaborador "${name}" e TODAS as coletas dele?\n\nIsto também BLOQUEIA o agente: mesmo instalado, ele não volta a enviar dados (dá para desbloquear depois).\n\nDigite EXCLUIR para confirmar:`);
  if ((typed || '').trim().toUpperCase() !== 'EXCLUIR') {
    if (typed !== null) toast('Confirmação não confere — exclusão cancelada.');
    return;
  }
  try {
    await apiFetch('DELETE', '/v1/users/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant) + '&block=true');
    toast('Colaborador excluído e agente bloqueado.');
    await loadCollabs();
    if (typeof loadBlocked === 'function') loadBlocked();
  } catch (e) { toast('Falha ao excluir: ' + e.message); }
}

async function loadBlocked() {
  const box = document.getElementById('blockedList');
  if (!box) return;
  if (!(state.role === 'admin_master' || state.role === 'wp_admin')) return;  // só Admin Master
  try {
    const d = await api('/v1/blocked-devices?tenant=' + encodeURIComponent(state.tenant));
    const list = d.blocked || [];
    if (!list.length) { box.innerHTML = '<p class="empty-state">Nenhum agente bloqueado.</p>'; return; }
    box.innerHTML = '<table><thead><tr><th>Device</th><th>Quando</th><th>Por</th><th></th></tr></thead><tbody>' +
      list.map(b => `<tr>
        <td><code style="font-size:11px;color:var(--text-dim)">${cleanUserId(b.user_id)}</code></td>
        <td style="font-size:12px;color:var(--text-dim)">${b.blocked_at ? relTime(b.blocked_at) : '—'}</td>
        <td style="font-size:12px;color:var(--text-dim)">${escHtml(b.blocked_by || '—')}</td>
        <td><button class="ghost" onclick="unblockDevice('${(b.user_id||'').replace(/'/g, "\\'")}')">Desbloquear</button></td>
      </tr>`).join('') + '</tbody></table>';
  } catch (e) {
    box.innerHTML = '<p class="empty-state">Erro: ' + escHtml(e.message) + '</p>';
  }
}

async function unblockDevice(uid) {
  if (!confirm('Desbloquear este agente? Ele volta a poder enviar coletas e o colaborador reaparece no próximo envio.')) return;
  try {
    await apiFetch('DELETE', '/v1/blocked-devices/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant));
    toast('Agente desbloqueado.');
    loadBlocked();
  } catch (e) { toast('Falha: ' + e.message); }
}

/* ---------- Config do agente: override por tenant (Admin Master) ---------- */
async function loadAgentCfgTenant() {
  if (!document.getElementById('taAfk')) return;
  if (!(state.role === 'admin_master' || state.role === 'wp_admin')) return;  // só Admin Master
  try {
    const d = await api('/v1/agent-config?tenant=' + encodeURIComponent(state.tenant));
    const g = d.global || {}, o = d.override || {};
    // placeholders mostram o valor global herdado; valor preenchido = override
    const setNum = (id, ov, gv) => {
      const el = document.getElementById(id); if (!el) return;
      el.placeholder = (gv != null ? 'global: ' + gv : 'herdar');
      el.value = (ov != null ? ov : '');
    };
    setNum('taAfk',  o.afk_timeout_sec,       g.afk_timeout_sec);
    setNum('taPoll', o.poll_interval_ms,      g.poll_interval_ms);
    setNum('taHb',   o.heartbeat_interval_sec, g.heartbeat_interval_sec);
    const tt = document.getElementById('taTitles');
    if (tt) tt.value = (o.capture_titles === true ? 'true' : o.capture_titles === false ? 'false' : '');
    const tm = document.getElementById('taMeet');
    if (tm) {
      tm.value = Array.isArray(o.meeting_patterns) ? o.meeting_patterns.join('\n') : '';
      tm.placeholder = Array.isArray(g.meeting_patterns) ? g.meeting_patterns.join('\n') : 'herdar do global';
    }
    const msg = document.getElementById('taMsg'); if (msg) msg.textContent = '';
  } catch (e) {
    const msg = document.getElementById('taMsg'); if (msg) msg.textContent = 'Erro ao carregar: ' + e.message;
  }
}

function _taNum(id) {
  const v = (document.getElementById(id).value || '').trim();
  return v === '' ? null : Number(v);
}

async function saveAgentCfgTenant() {
  const msg = document.getElementById('taMsg');
  const tt = document.getElementById('taTitles').value;
  const meetRaw = (document.getElementById('taMeet').value || '').trim();
  const body = {
    afk_timeout_sec:        _taNum('taAfk'),
    poll_interval_ms:       _taNum('taPoll'),
    heartbeat_interval_sec: _taNum('taHb'),
    capture_titles:         (tt === '' ? null : tt === 'true'),
    meeting_patterns:       (meetRaw === '' ? null : meetRaw.split('\n').map(s => s.trim()).filter(Boolean)),
  };
  try {
    await apiFetch('PUT', '/v1/agent-config?tenant=' + encodeURIComponent(state.tenant), body);
    if (msg) msg.textContent = 'Salvo. Os agentes aplicam no próximo refresh (~10 min) ou ao reiniciar.';
    loadAgentCfgTenant();
  } catch (e) { if (msg) msg.textContent = 'Falha ao salvar: ' + e.message; }
}

function openAgentCfgHelp()  { const m = document.getElementById('agentCfgHelpModal'); if (m) m.style.display = 'flex'; }
function closeAgentCfgHelp() { const m = document.getElementById('agentCfgHelpModal'); if (m) m.style.display = 'none'; }

async function clearAgentCfgTenant() {
  if (!confirm('Remover o override e voltar a herdar TODA a config do global?')) return;
  const msg = document.getElementById('taMsg');
  const body = { afk_timeout_sec:null, poll_interval_ms:null, heartbeat_interval_sec:null, capture_titles:null, meeting_patterns:null };
  try {
    await apiFetch('PUT', '/v1/agent-config?tenant=' + encodeURIComponent(state.tenant), body);
    if (msg) msg.textContent = 'Override removido — herdando o global.';
    loadAgentCfgTenant();
  } catch (e) { if (msg) msg.textContent = 'Falha: ' + e.message; }
}

/* ---------- Onboarding de agentes (enrollment) — Admin Master ---------- */
async function loadEnrollment() {
  if (!document.getElementById('enrollApproval')) return;
  if (!(state.role === 'admin_master' || state.role === 'wp_admin')) return;
  const t = encodeURIComponent(state.tenant);
  try {
    const s = await api('/v1/enroll-settings?tenant=' + t);
    document.getElementById('enrollApproval').checked = !!s.approval_required;
  } catch (_) {}
  _renderEnrollCodes();
  _renderEnrollPending();
}

async function _renderEnrollCodes() {
  const box = document.getElementById('enrollCodesList');
  if (!box) return;
  try {
    const d = await api('/v1/enroll-codes?tenant=' + encodeURIComponent(state.tenant));
    const list = d.codes || [];
    if (!list.length) { box.innerHTML = '<p class="empty-state">Nenhum código ativo. Gere um acima.</p>'; return; }
    box.innerHTML = '<table><thead><tr><th>Código</th><th>Rótulo</th><th>Validade</th><th>Status</th><th></th></tr></thead><tbody>' +
      list.map(c => {
        const exp = c.expires_at ? new Date(c.expires_at).toLocaleDateString('pt-BR') : 'permanente';
        const expired = c.expires_at && c.expires_at < new Date().toISOString();
        const st = !c.active ? 'revogado' : (expired ? 'expirado' : 'ativo');
        const stColor = st === 'ativo' ? '#16a34a' : 'var(--text-muted)';
        return `<tr>
          <td><code style="font-size:13px;font-weight:600">${escHtml(c.code)}</code>
              <button class="ghost" style="font-size:11px;padding:2px 6px" onclick="copyEnroll('${c.code}')">copiar</button></td>
          <td>${escHtml(c.label || '—')}</td>
          <td style="font-size:12px">${exp}</td>
          <td style="color:${stColor};font-size:12px">${st}</td>
          <td>${c.active ? `<button class="ghost" style="color:var(--danger)" onclick="revokeEnroll('${c.code}')">Revogar</button>` : ''}</td>
        </tr>`;
      }).join('') + '</tbody></table>';
  } catch (e) { box.innerHTML = '<p class="empty-state">Erro: ' + escHtml(e.message) + '</p>'; }
}

const _ENROLL_ST = {
  pending:  { label: 'Pendente',  color: '#d97706' },
  approved: { label: 'Aprovado',  color: '#16a34a' },
  rejected: { label: 'Rejeitado', color: 'var(--danger)' },
};

let _enrollDevs    = [];
let _enrollPage    = 0;
const _ENROLL_PP   = 8;   // agentes por página

async function _renderEnrollPending() {
  const box = document.getElementById('enrollPendingList');
  if (!box) return;
  try {
    const d = await api('/v1/enroll-devices?tenant=' + encodeURIComponent(state.tenant));  // todos os status
    _enrollDevs = d.devices || [];
    _enrollPage = 0;
    _paintEnroll();
  } catch (e) { box.innerHTML = '<p class="empty-state">Erro: ' + escHtml(e.message) + '</p>'; }
}

function _enrollGoPage(n) { _enrollPage = n; _paintEnroll(); }

function _paintEnroll() {
  const box = document.getElementById('enrollPendingList');
  if (!box) return;
  const list = _enrollDevs;
  if (!list.length) { box.innerHTML = '<p class="empty-state">Nenhum agente enrollado ainda.</p>'; return; }

  const totalPages = Math.max(1, Math.ceil(list.length / _ENROLL_PP));
  if (_enrollPage >= totalPages) _enrollPage = totalPages - 1;
  const start    = _enrollPage * _ENROLL_PP;
  const pageRows = list.slice(start, start + _ENROLL_PP);

  let html = '<table><thead><tr><th>Máquina</th><th>SO</th><th>Device</th><th>Status</th><th>Quando</th><th></th></tr></thead><tbody>' +
    pageRows.map(v => {
      const st = _ENROLL_ST[v.status] || { label: v.status, color: 'var(--text-muted)' };
      return `<tr>
        <td><strong>${escHtml(v.hostname || '—')}</strong></td>
        <td>${escHtml(v.os || '—')}</td>
        <td><code style="font-size:11px;color:var(--text-dim)">${escHtml(cleanUserId(v.device_id))}</code></td>
        <td><span style="color:${st.color};font-weight:600;font-size:12px">${st.label}</span></td>
        <td style="font-size:12px;color:var(--text-dim)">${v.requested_at ? relTime(v.requested_at) : '—'}</td>
        <td style="white-space:nowrap">
          ${v.status === 'pending' ? `<button class="primary" style="padding:3px 10px" onclick="decideEnroll(${v.id},'approve')">Aprovar</button>
            <button class="ghost" onclick="decideEnroll(${v.id},'reject')">Rejeitar</button>` : ''}
          <button class="ghost" style="color:var(--danger)" onclick="deleteEnrollDevice(${v.id},'${(v.hostname||v.device_id||'').replace(/'/g,'')}')">Excluir</button>
        </td>
      </tr>`;
    }).join('') + '</tbody></table>';

  if (totalPages > 1) {
    let nums = '';
    const s = Math.max(0, _enrollPage - 2), e = Math.min(totalPages - 1, s + 4);
    for (let i = s; i <= e; i++) {
      nums += `<button class="ghost ev-page-btn${i === _enrollPage ? ' ev-page-active' : ''}" onclick="_enrollGoPage(${i})">${i + 1}</button>`;
    }
    html += `<div class="ev-pagination" style="margin-top:10px">
      <button class="ghost ev-page-nav" ${_enrollPage > 0 ? '' : 'disabled'} onclick="_enrollGoPage(${_enrollPage - 1})">← Anterior</button>
      <div class="ev-page-numbers">${nums}</div>
      <button class="ghost ev-page-nav" ${_enrollPage + 1 < totalPages ? '' : 'disabled'} onclick="_enrollGoPage(${_enrollPage + 1})">Próxima →</button>
    </div>`;
  }
  box.innerHTML = html;
}

async function deleteEnrollDevice(id, name) {
  if (!confirm('Excluir o registro de enrollment de "' + name + '"?\n\nSe a máquina re-enrollar, passa pela fila de novo. Não revoga licença já entregue nem para coleta em andamento (para isso use "Agentes bloqueados").')) return;
  try { await apiFetch('DELETE', '/v1/enroll-devices/' + id + '?tenant=' + encodeURIComponent(state.tenant)); _renderEnrollPending(); }
  catch (e) { toast('Falha: ' + e.message); }
}

async function saveEnrollSettings() {
  const v = document.getElementById('enrollApproval').checked;
  try { await apiFetch('PUT', '/v1/enroll-settings?tenant=' + encodeURIComponent(state.tenant), { approval_required: v }); toast('Preferência salva.'); }
  catch (e) { toast('Falha: ' + e.message); }
}

async function createEnrollCode() {
  const label = (document.getElementById('enrollLabel').value || '').trim();
  const daysRaw = (document.getElementById('enrollDays').value || '').trim();
  const msg = document.getElementById('enrollMsg');
  const body = { label: label || null, days_valid: daysRaw ? parseInt(daysRaw, 10) : null };
  try {
    const r = await apiFetch('POST', '/v1/enroll-codes?tenant=' + encodeURIComponent(state.tenant), body);
    if (msg) msg.textContent = 'Código gerado: ' + r.code;
    document.getElementById('enrollLabel').value = '';
    document.getElementById('enrollDays').value = '';
    _renderEnrollCodes();
  } catch (e) { if (msg) msg.textContent = 'Falha: ' + e.message; }
}

async function revokeEnroll(code) {
  if (!confirm('Revogar o código ' + code + '? Novas instalações com ele param de funcionar (as já plugadas continuam).')) return;
  try { await apiFetch('DELETE', '/v1/enroll-codes/' + encodeURIComponent(code) + '?tenant=' + encodeURIComponent(state.tenant)); _renderEnrollCodes(); }
  catch (e) { toast('Falha: ' + e.message); }
}

async function decideEnroll(id, action) {
  try { await apiFetch('POST', '/v1/enroll-devices/' + id + '/decide?tenant=' + encodeURIComponent(state.tenant), { action }); _renderEnrollPending(); }
  catch (e) { toast('Falha: ' + e.message); }
}

function copyEnroll(code) {
  if (navigator.clipboard) navigator.clipboard.writeText(code).then(() => toast('Código copiado.'), () => {});
}
