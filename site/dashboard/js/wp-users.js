// =====================================================================
// wp-users.js — Gestão de usuários do painel + licença (Admin Master)
// Carregado após wp-settings.js; estende loadConfig().
// =====================================================================

function _duShowErr(msg) {
  const e = document.getElementById('duError');
  if (e) { e.textContent = msg; e.style.display = msg ? 'block' : 'none'; }
}

// Modal de ajuda: comparação dos perfis (ícone "?" ao lado de Perfil)
function openRoleHelp()  { const m = document.getElementById('roleHelpModal'); if (m) m.style.display = 'flex'; }
function closeRoleHelp() { const m = document.getElementById('roleHelpModal'); if (m) m.style.display = 'none'; }

const _DU_ROLE_LBL = { admin_master: 'Admin Master', admin: 'Admin', supervisor: 'Supervisor', lider: 'Líder', suporte: 'Suporte', wp_admin: 'WorkPulse' };

async function loadUsers() {
  const tb = document.querySelector('#duTable tbody');
  if (!tb) return;
  try {
    const data  = await api('/v1/tenant-users?tenant=' + encodeURIComponent(state.tenant));
    const users = data.users || [];
    state.dashUsers = users;
    if (!users.length) {
      tb.innerHTML = '<tr><td colspan="5" style="color:var(--text-muted);font-size:13px">Nenhum usuário cadastrado.</td></tr>';
      return;
    }
    tb.innerHTML = users.map(u => {
      const dis = u.status !== 'active';
      const statusHtml = dis
        ? '<span style="color:var(--danger)">Bloqueado</span>'
        : '<span style="color:#4ade80">Ativo</span>';
      const toggleBtn = dis
        ? `<button class="ghost" onclick="duUnblock(${u.id})">Reativar</button>`
        : `<button class="ghost" onclick="duBlock(${u.id})">Bloquear</button>`;
      return `<tr>
        <td>${escHtml(u.display_name || '—')}</td>
        <td>${escHtml(u.email)}</td>
        <td>${_DU_ROLE_LBL[u.role] || u.role}</td>
        <td>${statusHtml}</td>
        <td style="white-space:nowrap">
          <button class="ghost" onclick="duEdit(${u.id})">Editar</button>
          <button class="ghost" onclick="duReset(${u.id})">Reenviar senha</button>
          ${toggleBtn}
          <button class="ghost" onclick="duDelete(${u.id}, '${escHtml(u.email)}')">Excluir</button>
        </td></tr>`;
    }).join('');
  } catch (e) {
    tb.innerHTML = '<tr><td colspan="5" style="color:var(--danger);font-size:13px">Erro ao carregar: ' + escHtml(e.message) + '</td></tr>';
  }
}

async function duCreate() {
  _duShowErr('');
  const name  = document.getElementById('duName').value.trim();
  const email = document.getElementById('duEmail').value.trim();
  const role  = document.getElementById('duRole').value;
  if (!email) { _duShowErr('Informe o e-mail.'); return; }
  try {
    const res = await apiFetch('POST', '/v1/tenant-users?tenant=' + encodeURIComponent(state.tenant),
      { email, display_name: name, role });
    document.getElementById('duName').value  = '';
    document.getElementById('duEmail').value = '';
    _duShowLink(email, res && res.set_password_link);
    toast('Usuário cadastrado. Link para definir senha enviado por e-mail.');
    loadUsers();
  } catch (e) { _duShowErr('Falha: ' + (e.message || '')); }
}

// Exibe o link de senha para cópia/envio manual.
function _duShowLink(email, link) {
  const box = document.getElementById('duLinkBox');
  if (!box) return;
  if (!link) { box.style.display = 'none'; return; }
  const inp = document.getElementById('duLink');
  const forEl = document.getElementById('duLinkFor');
  if (forEl) forEl.textContent = email || '';
  if (inp) inp.value = link;
  box.style.display = 'block';
}

function duCopyLink() {
  const inp = document.getElementById('duLink');
  if (!inp || !inp.value) return;
  inp.select();
  if (navigator.clipboard) {
    navigator.clipboard.writeText(inp.value).then(() => toast('Link copiado.'), () => { document.execCommand('copy'); toast('Link copiado.'); });
  } else { document.execCommand('copy'); toast('Link copiado.'); }
}


// ── Editar usuário (perfil + visão do Líder) ──────────────────────────
let _duEditId = null, _duDeptList = [], _duCollabList = [], _duSelDepts = new Set(), _duSelUsers = new Set();

async function duEdit(id) {
  const u = (state.dashUsers || []).find(x => x.id === id);
  if (!u) return;
  _duEditId = id;
  document.getElementById('duEditName').textContent = u.email;
  document.getElementById('duEditDisplay').value = u.display_name || '';
  document.getElementById('duEditRole').value = (u.role === 'wp_admin') ? 'admin_master' : u.role;
  document.getElementById('duEditError').style.display = 'none';
  _duSelDepts = new Set(); _duSelUsers = new Set();
  try {
    const [deps, cols] = await Promise.all([
      api('/v1/departments?tenant=' + encodeURIComponent(state.tenant)),
      api('/v1/users?tenant=' + encodeURIComponent(state.tenant)),
    ]);
    _duDeptList   = (deps.departments || []).map(d => d.name);
    _duCollabList = (cols.users || []);
    if (u.role === 'lider') {
      const sc = await api(`/v1/tenant-users/${id}/scope?tenant=` + encodeURIComponent(state.tenant));
      _duSelDepts = new Set(sc.departments || []);
      _duSelUsers = new Set(sc.users || []);
    }
  } catch (_) {}
  duEditRoleChanged();
  document.getElementById('duEditModal').classList.remove('hide');
}

function duEditRoleChanged() {
  const isLider = document.getElementById('duEditRole').value === 'lider';
  document.getElementById('duScopeBox').style.display = isLider ? 'block' : 'none';
  if (isLider) { duScopeRenderDepts(); duScopeRenderUsers(); }
}

function duScopeRenderDepts() {
  const box = document.getElementById('duScopeDepts');
  if (!_duDeptList.length) { box.innerHTML = '<span style="color:var(--text-muted);font-size:12px">Nenhum departamento cadastrado.</span>'; return; }
  box.innerHTML = _duDeptList.map(d => {
    const ck = _duSelDepts.has(d) ? 'checked' : '';
    const esc = d.replace(/'/g, "\\'");
    return `<label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:4px 10px;cursor:pointer">
      <input type="checkbox" ${ck} onchange="_duToggleDept('${esc}', this.checked)"> ${escHtml(d)}</label>`;
  }).join('');
}
function _duToggleDept(d, on) { on ? _duSelDepts.add(d) : _duSelDepts.delete(d); }

function duScopeRenderUsers() {
  const box = document.getElementById('duScopeUsers');
  const q = (document.getElementById('duScopeFilter').value || '').toLowerCase();
  const list = _duCollabList.filter(u => !q
    || (u.display_name || '').toLowerCase().includes(q)
    || (u.department || '').toLowerCase().includes(q));
  if (!list.length) { box.innerHTML = '<span style="color:var(--text-muted);font-size:12px">Nenhum colaborador.</span>'; return; }
  box.innerHTML = list.map(u => {
    const ck = _duSelUsers.has(u.user_id) ? 'checked' : '';
    const esc = (u.user_id || '').replace(/'/g, "\\'");
    return `<label style="display:flex;align-items:center;gap:8px;font-size:13px;padding:4px 2px;cursor:pointer">
      <input type="checkbox" ${ck} onchange="_duToggleUser('${esc}', this.checked)">
      <span style="flex:1;min-width:0">${escHtml(u.display_name || u.user_id)}</span>
      <small style="color:var(--text-muted);white-space:nowrap">${escHtml(u.department || '')}</small></label>`;
  }).join('');
}
function _duToggleUser(uid, on) { on ? _duSelUsers.add(uid) : _duSelUsers.delete(uid); }

async function duEditSave() {
  const err = document.getElementById('duEditError');
  const display = document.getElementById('duEditDisplay').value.trim();
  const role = document.getElementById('duEditRole').value;
  try {
    await apiFetch('PUT', `/v1/tenant-users/${_duEditId}?tenant=` + encodeURIComponent(state.tenant),
      { display_name: display, role });
    if (role === 'lider') {
      await apiFetch('PUT', `/v1/tenant-users/${_duEditId}/scope?tenant=` + encodeURIComponent(state.tenant),
        { departments: [..._duSelDepts], users: [..._duSelUsers] });
    }
    document.getElementById('duEditModal').classList.add('hide');
    toast('Usuário atualizado.');
    loadUsers();
  } catch (e) { err.textContent = 'Falha: ' + (e.message || ''); err.style.display = 'block'; }
}

async function duBlock(id) {
  try { await apiFetch('POST', `/v1/tenant-users/${id}/block?tenant=` + encodeURIComponent(state.tenant)); toast('Usuário bloqueado.'); loadUsers(); }
  catch (e) { toast('Falha: ' + (e.message || '')); }
}
async function duUnblock(id) {
  try { await apiFetch('PUT', `/v1/tenant-users/${id}?tenant=` + encodeURIComponent(state.tenant), { status: 'active' }); toast('Usuário reativado.'); loadUsers(); }
  catch (e) { toast('Falha: ' + (e.message || '')); }
}
async function duReset(id) {
  try {
    const res = await apiFetch('POST', `/v1/tenant-users/${id}/reset-password?tenant=` + encodeURIComponent(state.tenant));
    const u = (state.dashUsers || []).find(x => x.id === id);
    _duShowLink(u ? u.email : '', res && res.reset_password_link);
    toast('Link de redefinição enviado por e-mail.');
  } catch (e) { toast('Falha: ' + (e.message || '')); }
}
async function duDelete(id, email) {
  if (!confirm('Excluir o acesso de ' + email + '? O login será desativado.')) return;
  try { await apiFetch('DELETE', `/v1/tenant-users/${id}?tenant=` + encodeURIComponent(state.tenant)); toast('Acesso excluído.'); loadUsers(); }
  catch (e) { toast('Falha: ' + (e.message || '')); }
}

// ── Licença ──────────────────────────────────────────────────────────
async function loadLicense() {
  const sum      = document.getElementById('licSummary');
  const seatsBox = document.getElementById('licSeats');
  if (!sum) return;
  try {
    const d   = await api('/v1/tenant-license?tenant=' + encodeURIComponent(state.tenant));
    const lic = d.license || {};
    const used = d.used || 0, seats = d.seats || 0, avail = (d.available != null ? d.available : Math.max(0, seats - used));
    // Validade com ANO + dias restantes (vermelho se vencida, âmbar se <= 30 dias)
    let expStr = '—', expColor = 'inherit', expLabel = '';
    if (lic.expires) {
      const exp = new Date(lic.expires);
      expStr = exp.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
      const daysLeft = Math.ceil((exp - Date.now()) / 86400000);
      if (daysLeft < 0)        { expColor = 'var(--danger)';  expLabel = ' (vencida)'; }
      else if (daysLeft <= 30) { expColor = '#f59e0b';        expLabel = ` (${daysLeft} dias)`; }
      else                     { expLabel = ` (${daysLeft} dias)`; }
    }
    sum.innerHTML =
      `<span><strong>Plano:</strong> ${escHtml(lic.plan || '—')}</span>` +
      `<span><strong>Colaboradores:</strong> ${used} / ${seats}</span>` +
      `<span><strong>Disponíveis:</strong> ${avail}</span>` +
      `<span style="color:${expColor}"><strong>Validade:</strong> ${expStr}${expLabel}</span>`;
    if (seatsBox) {
      const pct = seats ? Math.round((used / seats) * 100) : 0;
      const barW = Math.min(100, pct);
      seatsBox.innerHTML =
        `<div style="font-size:13px;color:var(--text-dim);margin-bottom:6px">${used} de ${seats} assentos em uso (${pct}%) — contagem automática pelos colaboradores monitorados.</div>` +
        `<div style="height:8px;background:var(--bg-1,#0f1530);border:1px solid var(--border);border-radius:6px;overflow:hidden;max-width:420px">
           <div style="height:100%;width:${barW}%;background:var(--primary,#3b5bdb)"></div>
         </div>`;
    }
  } catch (e) {
    sum.innerHTML = '<span style="color:var(--danger)">Erro ao carregar licença: ' + escHtml(e.message) + '</span>';
  }
  // A página Configurações também carrega a fonte de dados de RH.
  if (typeof loadDirectoryConfig === 'function') loadDirectoryConfig();
}

/* ──────────────────────────────────────────────────────────────────
   FONTE DE DADOS DE RH (Convenia hoje; Google em breve) — Admin Master
   ────────────────────────────────────────────────────────────────── */
function dirProviderChange() {
  const prov = (document.getElementById('dirProvider') || {}).value;
  const tw = document.getElementById('dirConveniaTokenWrap');
  if (tw) tw.style.display = (prov === 'convenia') ? '' : 'none';
}

function _dirRenderStatus(cfg) {
  const el = document.getElementById('dirStatus');
  if (!el) return;
  if (!cfg || !cfg.last_sync) { el.textContent = 'Nunca sincronizado.'; return; }
  const when = new Date(cfg.last_sync).toLocaleString('pt-BR',
    { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const st = cfg.last_status || '';
  const err = /^erro/i.test(st);
  el.innerHTML = `Última sincronização: ${when}` +
    (st ? ` — <span style="color:${err ? 'var(--danger)' : 'var(--accent,#34d399)'}">${escHtml(st)}</span>` : '');
}

async function loadDirectoryConfig() {
  const sel = document.getElementById('dirProvider');
  if (!sel) return;
  try {
    const cfg = await api('/v1/tenant-directory/config?tenant=' + encodeURIComponent(state.tenant));
    sel.value = cfg.provider || 'none';
    const auto = document.getElementById('dirAutoSync');
    if (auto) auto.checked = cfg.auto_sync !== false;
    const tokState = document.getElementById('dirTokenState');
    const tokInput = document.getElementById('dirConveniaToken');
    if (cfg.has_token) {
      if (tokState) tokState.textContent = '· token salvo (deixe em branco p/ manter)';
      if (tokInput) tokInput.placeholder = '•••••••• (mantém o atual)';
    } else {
      if (tokState) tokState.textContent = '';
      if (tokInput) tokInput.placeholder = 'cole o token…';
    }
    dirProviderChange();
    _dirRenderStatus(cfg);
  } catch (e) {
    const st = document.getElementById('dirStatus');
    if (st) st.innerHTML = '<span style="color:var(--danger)">Erro ao carregar configuração: ' + escHtml(e.message) + '</span>';
  }
}

async function saveDirectoryConfig() {
  const provider  = (document.getElementById('dirProvider') || {}).value || 'none';
  const auto_sync = !!(document.getElementById('dirAutoSync') || {}).checked;
  const token     = (document.getElementById('dirConveniaToken') || {}).value || '';
  const body = { provider, auto_sync };
  if (token.trim()) body.convenia_token = token.trim();   // só envia se digitou
  try {
    await apiFetch('PUT', '/v1/tenant-directory/config?tenant=' + encodeURIComponent(state.tenant), body);
    const tokInput = document.getElementById('dirConveniaToken');
    if (tokInput) tokInput.value = '';                    // não deixa o token na tela
    toast('Fonte de dados salva.');
    loadDirectoryConfig();
  } catch (e) { toast('Erro ao salvar: ' + (e.message || '')); }
}

async function syncDirectoryNow() {
  const st = document.getElementById('dirStatus');
  if (st) st.textContent = 'Sincronizando… (pode levar alguns segundos)';
  try {
    const r = await apiFetch('POST', '/v1/tenant-directory/sync?tenant=' + encodeURIComponent(state.tenant), {});
    if (r && r.status === 'skipped') toast('Nenhuma fonte configurada.');
    else toast(`Sincronizado: ${r.matched || 0} colaboradores atualizados.`);
    loadDirectoryConfig();
  } catch (e) {
    toast('Falha na sincronização: ' + (e.message || ''));
    loadDirectoryConfig();
  }
}

// Parseia o textarea do DE-PARA de fotos em pares {key, url}. Aceita:
//   "nome ; url" / "email, url" / "nome<TAB>url"  OU  nome numa linha e url na seguinte.
function _parsePhotoMap(text) {
  const lines = (text || '').split('\n').map(s => s.trim()).filter(Boolean);
  const items = [];
  let pendingKey = null;
  for (const line of lines) {
    const m = line.match(/https?:\/\/\S+/);
    if (m) {
      const url = m[0];
      let key = line.slice(0, m.index).replace(/[\s,;|\t-]+$/, '').trim();
      if (!key) key = pendingKey;
      // Empurra mesmo sem key: o backend casa pelo UUID embutido no link.
      items.push({ key: key || '', url });
      pendingKey = null;
    } else {
      pendingKey = line;   // linha só com nome; o link vem na próxima
    }
  }
  return items;
}

async function importPhotos() {
  const ta      = document.getElementById('photoMapInput');
  const status  = document.getElementById('photoMapStatus');
  const unmatch = document.getElementById('photoMapUnmatched');
  const copy    = !!(document.getElementById('photoStoreCopy') || {}).checked;
  const items   = _parsePhotoMap(ta ? ta.value : '');
  if (unmatch) unmatch.textContent = '';
  if (!items.length) { toast('Cole ao menos uma linha com nome/e-mail e link.'); return; }
  if (status) status.textContent = copy
    ? `Baixando e importando ${items.length} foto(s)…`
    : `Importando ${items.length} link(s)…`;
  try {
    const r = await apiFetch('POST',
      '/v1/tenant-directory/photos?tenant=' + encodeURIComponent(state.tenant),
      { items, store_copy: copy });
    if (status) status.textContent = `${r.matched || 0} de ${r.total || items.length} casados.`;
    toast(`Fotos importadas: ${r.matched || 0} colaboradores.`);
    if (r.unmatched && r.unmatched.length && unmatch) {
      unmatch.textContent = 'Não encontrados (confira nome/e-mail): ' + r.unmatched.join(' · ');
    }
  } catch (e) {
    if (status) status.textContent = '';
    toast('Falha ao importar fotos: ' + (e.message || ''));
  }
}

async function licAssign(userId, allocated) {
  try {
    await apiFetch('POST', '/v1/tenant-license/assign?tenant=' + encodeURIComponent(state.tenant),
      { user_id: userId, allocated });
    loadLicense();
  } catch (e) { toast('Falha: ' + (e.message || '')); loadLicense(); }
}

async function licRequest() {
  const plan    = document.getElementById('licReqPlan').value.trim();
  const seatsRaw = document.getElementById('licReqSeats').value;
  const seats   = parseInt(seatsRaw, 10);
  const message = document.getElementById('licReqMsg').value.trim();
  try {
    await apiFetch('POST', '/v1/tenant-license/request?tenant=' + encodeURIComponent(state.tenant),
      { plan: plan || null, seats: isNaN(seats) ? null : seats, message: message || null });
    toast('Solicitação enviada à WorkPulse.');
    document.getElementById('licReqMsg').value = '';
  } catch (e) { toast('Falha: ' + (e.message || '')); }
}


// ── Financeiro (Admin Master) ─────────────────────────────────────────
function _finBRL(cents) {
  return 'R$ ' + ((cents || 0) / 100).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function loadFinance() {
  const sum = document.getElementById('finSummary');
  const tb  = document.querySelector('#finPaymentsTable tbody');
  if (!sum) return;
  try {
    const d = await api('/v1/tenant-finance?tenant=' + encodeURIComponent(state.tenant));
    const seats = d.seats || 0, used = d.used || 0;
    let expStr = '—';
    if (d.expires) {
      expStr = new Date(d.expires).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
    }
    sum.innerHTML =
      `<span><strong>Plano:</strong> ${escHtml(d.plan || '—')}</span>` +
      `<span><strong>Assentos contratados:</strong> ${seats}</span>` +
      `<span><strong>Em uso:</strong> ${used}</span>` +
      `<span><strong>Preço por assento:</strong> ${_finBRL(d.seat_price_cents)}${d.discount_pct ? ` (${d.discount_pct}% desc.)` : ''}</span>` +
      `<span><strong>Validade:</strong> ${expStr}</span>`;

    const mEl = document.getElementById('finMonthly');
    const cEl = document.getElementById('finMonthlyCalc');
    if (mEl) mEl.textContent = _finBRL(d.monthly_cents);
    if (cEl) cEl.textContent = `${_finBRL(d.seat_price_cents)} × ${seats} assentos`;

    const pays = d.payments || [];
    if (tb) {
      tb.innerHTML = pays.length ? pays.map(p => `<tr>
        <td>${escHtml(p.competencia)}</td>
        <td class="num">${_finBRL(p.amount_cents)}</td>
        <td>${p.paid_at ? escHtml(p.paid_at) : '—'}</td>
        <td>${escHtml(p.method || '—')}</td>
        <td>${escHtml(p.note || '—')}</td>
      </tr>`).join('') : '<tr><td colspan="5" class="empty-state">Nenhum pagamento registrado ainda.</td></tr>';
    }
  } catch (e) {
    sum.innerHTML = '<span style="color:var(--danger)">Erro ao carregar financeiro: ' + escHtml(e.message) + '</span>';
    if (tb) tb.innerHTML = '<tr><td colspan="5" class="empty-state">—</td></tr>';
  }
}

