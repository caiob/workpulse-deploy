// =====================================================================
// wp-users.js — Gestão de usuários do painel + licença (Admin Master)
// Carregado após wp-settings.js; estende loadConfig().
// =====================================================================

function _duShowErr(msg) {
  const e = document.getElementById('duError');
  if (e) { e.textContent = msg; e.style.display = msg ? 'block' : 'none'; }
}

const _DU_ROLE_LBL = { admin_master: 'Admin Master', supervisor: 'Supervisor', wp_admin: 'WorkPulse' };

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
    sum.innerHTML =
      `<span><strong>Plano:</strong> ${escHtml(lic.plan || '—')}</span>` +
      `<span><strong>Assentos:</strong> ${d.allocated} / ${d.seats}</span>` +
      `<span><strong>Disponíveis:</strong> ${d.available}</span>` +
      `<span><strong>Validade:</strong> ${lic.expires ? ptDate(lic.expires) : '—'}</span>`;
    const cs = d.colaboradores || [];
    seatsBox.innerHTML = cs.length
      ? cs.map(c => `<label style="display:inline-flex;align-items:center;gap:6px;margin:4px 14px 4px 0;font-size:13px">
          <input type="checkbox" ${c.allocated ? 'checked' : ''} onchange="licAssign('${escHtml(c.user_id)}', this.checked)">
          ${escHtml(c.display_name || c.user_id)}
        </label>`).join('')
      : '<span style="color:var(--text-muted);font-size:13px">Nenhum colaborador encontrado.</span>';
  } catch (e) {
    sum.innerHTML = '<span style="color:var(--danger)">Erro ao carregar licença: ' + escHtml(e.message) + '</span>';
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

// ── Estende loadConfig() para carregar Usuários + Licença (só Admin Master) ──
(function () {
  const orig = window.loadConfig;
  window.loadConfig = function () {
    if (typeof orig === 'function') orig();
    if (state.role === 'admin_master' || state.role === 'wp_admin') {
      loadUsers();
      loadLicense();
    }
  };
})();
