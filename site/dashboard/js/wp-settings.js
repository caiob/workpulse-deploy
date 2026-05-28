// =====================================================================
// wp-settings.js — Aba "Configurações"
// Inclui: regras de produtividade, apps detectados, departamentos,
//         colaboradores e toggle de tema.
// =====================================================================

let _allApps = [];
let _depts   = []; // cache local: [{id, name, user_count, users:[...]}]

/* -------------------- LOAD CONFIG -------------------- */
async function loadConfig() {
  await Promise.all([loadRules(), loadAppsForConfig(), loadCollabs(), loadDepts(), loadReports()]);
  // Atualiza botão de tema (pode ter mudado de aba)
  const btn = document.getElementById('themeToggleBtn');
  if (btn) {
    const isLight = document.body.classList.contains('theme-light');
    btn.textContent = isLight ? '🌙 Tema Escuro' : '☀️ Tema Claro';
  }
}

/* -------------------- REGRAS -------------------- */
async function loadRules() {
  try {
    const data = await api('/v1/productivity/rules?tenant=' + encodeURIComponent(state.tenant));
    renderRulesTable(data.rules || []);
  } catch (e) { toast('Erro ao carregar regras: ' + e.message); }
}

function renderRulesTable(rules) {
  const tbody      = document.querySelector('#rulesTable tbody');
  const fieldLabel = { title: 'Título', app: 'Aplicativo' };
  const typeLabel  = { contains: 'Contém', exact: 'Exato', startswith: 'Começa com' };

  if (!rules.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">Nenhuma regra cadastrada ainda.<br><small>Use o formulário acima para criar a primeira.</small></td></tr>';
    return;
  }
  tbody.innerHTML = rules.map(r =>
    `<tr>
      <td><strong>${r.label}</strong></td>
      <td><span class="pill">${fieldLabel[r.match_field] || r.match_field}</span></td>
      <td><code style="color:var(--text-dim)">${r.pattern}</code></td>
      <td style="color:var(--text-dim);font-size:12px">${typeLabel[r.match_type] || r.match_type}</td>
      <td>${CAT_PILLS[r.category] || r.category}</td>
      <td><button class="del-btn" onclick="deleteRule(${r.id})">Excluir</button></td>
    </tr>`
  ).join('');
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

/* -------------------- APPS DETECTADOS -------------------- */
async function loadAppsForConfig() {
  try {
    const data = await api('/v1/productivity/apps?tenant=' + encodeURIComponent(state.tenant) + '&days=30');
    _allApps = data.apps || [];
    renderAppsList(_allApps);
  } catch (e) { toast('Erro ao carregar apps: ' + e.message); }
}

function renderAppsList(apps) {
  const el = document.getElementById('appsList');
  if (!apps.length) {
    el.innerHTML = '<p style="color:var(--text-muted);font-size:13px">Nenhum aplicativo detectado no período.</p>';
    return;
  }
  const catDot = { productive: '🟢', unproductive: '🔴', neutral: '⚪', uncategorized: '🟡' };
  el.innerHTML = '<div class="chip-grid">' + apps.map(a => {
    const icon     = a.is_browser ? '🌐' : '📱';
    const dot      = catDot[a.category] || catDot.uncategorized;
    const safeName = (a.app || '').replace(/'/g, "\\'");
    const field    = a.is_browser ? 'title' : 'app';
    return `<span class="chip${a.is_browser ? ' browser' : ''}"
      onclick="quickRule('${safeName}', '${field}')"
      title="${fmtSec(a.total_sec)} - ${a.user_count} usuário(s)">
      ${dot} ${icon} ${a.app}
      <span class="chip-time">${fmtSec(a.total_sec)}</span>
    </span>`;
  }).join('') + '</div>';
}

function filterApps() {
  const q        = (document.getElementById('appsFilter').value || '').toLowerCase();
  const filtered = q ? _allApps.filter(a => (a.app || '').toLowerCase().includes(q)) : _allApps;
  renderAppsList(filtered);
}

/* -------------------- DEPARTAMENTOS (gestão) -------------------- */
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

/* -------------------- COLABORADORES -------------------- */
async function loadCollabs() {
  try {
    const data = await api('/v1/users?tenant=' + encodeURIComponent(state.tenant));
    state.collabs = data.users || [];
    renderCollabsTable(state.collabs);
  } catch (e) { toast('Erro ao carregar colaboradores: ' + e.message); }
}

function renderCollabsTable(users) {
  const tb = document.querySelector('#collabsTable tbody');
  if (!users.length) {
    tb.innerHTML = '<tr><td colspan="7" class="empty-state">Nenhum colaborador encontrado.</td></tr>';
    return;
  }
  tb.innerHTML = users.map(u => {
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
