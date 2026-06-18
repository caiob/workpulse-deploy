// =====================================================================
// wp-support.js — Aba "Suporte Técnico" (Admin Master + perfil Suporte)
// Esquerda: lista de colaboradores (paginada). Clicar no nome → detalhes
// da máquina e das últimas coletas no card da direita.
// =====================================================================

let _supUsers      = [];
let _supPage       = 0;
let _SUP_PAGE_SIZE = 10;
let _supSelected   = '';

async function loadSupport() {
  try {
    const d = await api('/v1/support/people?tenant=' + encodeURIComponent(state.tenant));
    _supUsers = d.users || [];
    _supPage  = 0;
    _supRender();
  } catch (e) { toast('Erro ao carregar Suporte: ' + e.message); }
}

function _supStatus(lastSeen) {
  const online = (typeof _isOnline === 'function') && _isOnline(lastSeen);
  const c = online ? '#4ade80' : 'var(--text-muted)';
  return `<span style="display:inline-flex;align-items:center;gap:5px;color:${c}">
    <span style="width:8px;height:8px;border-radius:50%;background:${c};display:inline-block"></span>${online ? 'Online' : 'Offline'}</span>`;
}

function _supRender() {
  const tbody = document.querySelector('#supTable tbody');
  if (!tbody) return;
  const q = (document.getElementById('supFilter')?.value || '').toLowerCase();

  const rows = _supUsers.filter(u => {
    const dn = (u.display_name || u.user_id || '').toLowerCase();
    return !q || dn.includes(q)
      || (u.user_id    || '').toLowerCase().includes(q)
      || (u.department || '').toLowerCase().includes(q)
      || (u.hostname   || '').toLowerCase().includes(q);
  });

  const total = rows.length;
  if (!total) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">Nenhum colaborador.</td></tr>';
    _renderCfgPagination('supPagination', 'supCountLabel', 0, 0, _SUP_PAGE_SIZE, '_supGoTo', 'colaboradores');
    return;
  }

  const pages = Math.ceil(total / _SUP_PAGE_SIZE);
  if (_supPage >= pages) _supPage = pages - 1;
  if (_supPage < 0) _supPage = 0;
  const start = _supPage * _SUP_PAGE_SIZE;
  const slice = rows.slice(start, start + _SUP_PAGE_SIZE);

  tbody.innerHTML = slice.map(u => {
    const name = u.display_name || u.user_id || '—';
    const safe = (u.user_id || '').replace(/'/g, "\\'");
    const sel  = (u.user_id === _supSelected) ? ' style="background:var(--bg-2)"' : '';
    return `<tr${sel}>
      <td><span class="person-link" onclick="_supSelect('${safe}')"><strong>${escHtml(name)}</strong></span></td>
      <td>${_supStatus(u.last_seen)}</td>
      <td style="font-size:12px;color:var(--text-dim)">${typeof relTime === 'function' ? relTime(u.last_seen) : (u.last_seen || '—')}</td>
      <td><code style="font-size:11px;color:var(--text-dim)">${escHtml(u.agent_version || '—')}</code></td>
    </tr>`;
  }).join('');

  _renderCfgPagination('supPagination', 'supCountLabel', _supPage, total, _SUP_PAGE_SIZE, '_supGoTo', 'colaboradores');
}

function _supGoTo(p)    { _supPage = p; _supRender(); }
function _supSetSize(v) { _SUP_PAGE_SIZE = Math.max(1, parseInt(v, 10) || 10); _supPage = 0; _supRender(); }

async function _supSelect(uid) {
  _supSelected = uid;
  _supRender();   // realça a linha selecionada
  const box = document.getElementById('supDetail');
  if (box) box.innerHTML = '<div class="empty-state">Carregando…</div>';
  try {
    const d = await api('/v1/support/person?tenant=' + encodeURIComponent(state.tenant) + '&user_id=' + encodeURIComponent(uid));
    _supRenderDetail(d);
  } catch (e) {
    if (box) box.innerHTML = '<div class="empty-state">Falha ao carregar: ' + escHtml(e.message) + '</div>';
  }
}

function _supRenderDetail(d) {
  const box = document.getElementById('supDetail');
  if (!box) return;
  const dtv = v => v ? (typeof ptDateTime === 'function' ? ptDateTime(v) : v) : '—';
  const rel = v => (v && typeof relTime === 'function') ? ` <span style="color:var(--text-dim);font-size:11px">(${relTime(v)})</span>` : '';
  const row = (label, val) => `<div style="display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--border)">
      <span style="color:var(--text-dim);font-size:13px">${label}</span>
      <span style="text-align:right;font-size:13px">${val}</span></div>`;
  const head = (t) => `<div style="margin:16px 0 2px;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">${t}</div>`;

  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <h3 style="margin:0">${escHtml(d.display_name || d.user_id)}</h3>
      ${_supStatus(d.last_seen)}
    </div>
    ${row('Colaborador', escHtml(d.display_name || d.user_id || '—'))}
    ${row('Departamento', escHtml(d.department || '—'))}
    ${row('Nome da máquina', `<code style="font-size:12px">${escHtml(d.hostname || '—')}</code>`)}
    ${row('Versão do agente', `<code style="font-size:12px">${escHtml(d.agent_version || '—')}</code>`)}
    ${row('Sistema', escHtml([d.os, d.os_version].filter(Boolean).join(' ') || '—'))}
    ${head('Última coleta')}
    ${row('AFK',        dtv(d.last_afk) + rel(d.last_afk))}
    ${row('Aplicativo', dtv(d.last_app) + rel(d.last_app))}
    ${row('Web',        dtv(d.last_web) + rel(d.last_web))}
    ${head('Jornada')}
    ${row('Início da jornada (hoje)', dtv(d.jornada_start))}
  `;
}
