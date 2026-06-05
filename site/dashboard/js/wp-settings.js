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
    const r = await fetch(
      state.url + '/v1/productivity/rules?tenant=' + encodeURIComponent(state.tenant),
      {
        method: 'POST',
        headers: { 'X-Admin-Token': state.token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, pattern, match_field, match_type, category }),
      }
    );
    if (!r.ok) { const t = await r.text(); throw new Error(t); }
    toast('Regra salva com sucesso!');
    document.getElementById('ruleLabel').value   = '';
    document.getElementById('rulePattern').value = '';
    await loadRules();
  } catch (e) { toast('Erro ao salvar regra: ' + e.message); }
}

async function deleteRule(id) {
  if (!confirm('Excluir esta regra?')) return;
  try {
    const r = await fetch(
      state.url + '/v1/productivity/rules/' + id + '?tenant=' + encodeURIComponent(state.tenant),
      { method: 'DELETE', headers: { 'X-Admin-Token': state.token } }
    );
    if (!r.ok) { const t = await r.text(); throw new Error(t); }
    toast('Regra excluída.');
    await loadRules();
  } catch (e) { toast('Erro ao excluir regra: ' + e.message); }
}

function quickRule(pattern, field) {
  switchTab('config');
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

  const slice = users.slice(_collabsPage * _COLLABS_PAGE_SIZE, (_collabsPage + 1) * _COLLABS_PAGE_SIZE);
  tb.innerHTML = slice.map(u => {
    const uid     = u.user_id || '';
    const safeUid = uid.replace(/'/g, "\\'");
    return `<tr>
      <td><code style="color:var(--text-dim);font-size:11px">${cleanUserId(uid)}</code></td>
      <td>${u.display_name || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${u.email        || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${u.department   || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${u.role         || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td style="color:var(--text-dim);font-size:12px">${relTime(u.last_seen)}</td>
      <td><button class="cat-btn" onclick="editUser('${safeUid}')">✏ Editar</button></td>
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
    if (tab !== 'config') switchTab('config');
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
    const r = await fetch(
      state.url + '/v1/users/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant),
      {
        method:  'PATCH',
        headers: { 'X-Admin-Token': state.token, 'Content-Type': 'application/json' },
        body:    JSON.stringify(body),
      }
    );
    if (!r.ok) { const t = await r.text(); throw new Error(t); }
    toast('Colaborador atualizado!');
    document.getElementById('collabForm').style.display = 'none';
    await loadCollabs();
  } catch (e) { toast('Erro ao salvar: ' + e.message); }
}

// Alias — chamado pela aba Internet para pré-preencher regra de site
function prefillRule(siteName) { quickRule(siteName, "site"); }
