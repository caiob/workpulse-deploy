// =====================================================================
// wp-support.js — Aba "Suporte Técnico" (Admin Master + perfil Suporte)
// Esquerda: lista de colaboradores (paginada). Clicar no nome → detalhes
// da máquina e das últimas coletas no card da direita.
// =====================================================================

let _supUsers      = [];
let _supPage       = 0;
let _SUP_PAGE_SIZE = 10;
let _supSelected   = '';
let _supSort       = { key: null, dir: 1 };  // ordenação da lista (name/status/seen/ver)

async function loadSupport() {
  try {
    const d = await api('/v1/support/people?tenant=' + encodeURIComponent(state.tenant));
    _supUsers = d.users || [];
    markVacation(_supUsers);
    _supPage  = 0;
    _supRender();
    _supRenderDist();   // card consolidado (SO + versão do agente) — client-side, sem query extra
  } catch (e) { toast('Erro ao carregar Suporte: ' + e.message); }
  _supLoadAgentDownload();   // card de download do agente (VERSÃO ATUAL)
}

function _supStatus(lastSeen) {
  const online = (typeof _isOnline === 'function') && _isOnline(lastSeen);
  const c = online ? '#4ade80' : 'var(--text-muted)';
  return `<span style="display:inline-flex;align-items:center;gap:5px;color:${c}">
    <span style="width:8px;height:8px;border-radius:50%;background:${c};display:inline-block"></span>${online ? 'Online' : 'Offline'}</span>`;
}

// Estoque: máquina sem usuário. Se voltou a coletar (online), avisa — a flag NÃO sai
// sozinha (decisão 2026-11-18); quem decide é o Suporte/Admin Master.
function _supStockStatus(lastSeen) {
  const online = (typeof _isOnline === 'function') && _isOnline(lastSeen);
  return online
    ? '<span title="Marcada como estoque, mas está enviando coletas agora. Se foi entregue a alguém, remova do estoque." style="display:inline-flex;align-items:center;gap:5px;color:#b45309;font-weight:600"><span style="width:8px;height:8px;border-radius:50%;background:#f59e0b;display:inline-block"></span>📦 Estoque · coletando</span>'
    : '<span style="display:inline-flex;align-items:center;gap:5px;color:#92400e"><span style="width:8px;height:8px;border-radius:50%;background:#fbbf24;display:inline-block"></span>📦 Estoque</span>';
}

async function _supSetStock(uid, val) {
  const u = _supUsers.find(x => x.user_id === uid);
  const nome = (u && (u.display_name || u.user_id)) || uid;
  const msg = val
    ? `Marcar "${nome}" como máquina de ESTOQUE?\n\nEla sai da Visão Geral, do Tempo Ativo e dos alertas (e deixa de constar como férias, se estiver).`
    : `Remover "${nome}" do estoque? Ela volta a contar normalmente.`;
  if (!confirm(msg)) return;
  try {
    await apiFetch('PATCH',
      '/v1/users/' + encodeURIComponent(uid) + '?tenant=' + encodeURIComponent(state.tenant),
      { in_stock: !!val });
    if (u) { u.in_stock = !!val; if (val) u.on_vacation = false; }
    markVacation(_supUsers);
    toast(val ? 'Marcada como estoque.' : 'Removida do estoque.');
    _supRender();
    _supSelect(uid);
  } catch (e) { toast('Erro: ' + e.message); }
}

// Excluir o colaborador e liberar a máquina p/ estoque (Suporte + Admin Master).
// O agente segue ativo; apaga coletas e dados da pessoa; máquina fica "📦 Estoque".
async function _supReleaseToStock(uid) {
  const u = _supUsers.find(x => x.user_id === uid);
  const nome = (u && (u.display_name || u.user_id)) || uid;
  const maq  = (typeof cleanUserId === 'function') ? cleanUserId(uid) : uid;
  const typed = prompt(
    `Excluir o colaborador "${nome}" e liberar a máquina ${maq} para ESTOQUE?\n\n` +
    `• Apaga TODAS as coletas e os dados da pessoa (nome, e-mail, departamento).\n` +
    `• A máquina continua cadastrada, com o agente ATIVO (não é bloqueada).\n` +
    `• Para entregar a outra pessoa: Colaboradores → Editar → preencher os dados e Situação "Em uso".\n\n` +
    `Digite LIBERAR para confirmar:`);
  if ((typed || '').trim().toUpperCase() !== 'LIBERAR') {
    if (typed !== null) toast('Confirmação não confere — nada foi alterado.');
    return;
  }
  try {
    await apiFetch('POST',
      '/v1/users/' + encodeURIComponent(uid) + '/release-to-stock?tenant=' + encodeURIComponent(state.tenant));
    toast('Colaborador excluído — máquina liberada para estoque.');
    await loadSupport();
    _supSelect(uid);
  } catch (e) { toast('Falha ao liberar: ' + e.message); }
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

  if (_supSort.key) {
    const dir = _supSort.dir;
    rows.sort((a, b) => {
      let r = 0;
      if      (_supSort.key === 'name')   r = (a.display_name || a.user_id || '').localeCompare(b.display_name || b.user_id || '', 'pt-BR');
      else if (_supSort.key === 'status') r = (_supIsOn(a) ? 1 : 0) - (_supIsOn(b) ? 1 : 0);
      else if (_supSort.key === 'seen')   r = _supSeenTime(a.last_seen) - _supSeenTime(b.last_seen);
      else if (_supSort.key === 'ver')    r = _supVerCmp(a.agent_version || '', b.agent_version || '');
      return r * dir;
    });
  }
  _supUpdateSortIndicators();

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
    const stat = u.in_stock
      ? _supStockStatus(u.last_seen)
      : u.on_vacation
      ? '<span style="display:inline-flex;align-items:center;gap:5px;color:#0e7490"><span style="width:8px;height:8px;border-radius:50%;background:#22d3ee;display:inline-block"></span>🏖 Férias</span>'
      : _supStatus(u.last_seen);
    return `<tr${sel}>
      <td><span class="person-link" onclick="_supSelect('${safe}')"><strong>${escHtml(name)}</strong></span>${vacBadge(u.user_id)}</td>
      <td>${stat}</td>
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

  const _su = _supUsers.find(x => x.user_id === d.user_id) || {};
  const _inStock = !!_su.in_stock;
  const _safeUid = String(d.user_id || '').replace(/'/g, "\\'");
  const _stockBtn = (typeof canMarkStock === 'function' && canMarkStock() && d.user_id)
    ? `<span style="margin-left:auto;display:inline-flex;gap:6px;flex-wrap:wrap">
         <button class="ghost" onclick="_supSetStock('${_safeUid}', ${!_inStock})">${_inStock ? '↩ Remover do estoque' : '📦 Marcar como estoque'}</button>
         ${(_su.display_name || _su.department || !_inStock)
           ? `<button class="btn-danger-outline" title="Apaga coletas e dados da pessoa; a máquina fica em estoque com o agente ativo" onclick="_supReleaseToStock('${_safeUid}')">🗑 Excluir e liberar p/ estoque</button>`
           : ''}
       </span>`
    : '';
  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <h3 style="margin:0">${escHtml(d.display_name || d.user_id)}</h3>
      ${_inStock ? _supStockStatus(d.last_seen) : _supStatus(d.last_seen)}
      ${_stockBtn}
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

// ── Ordenação da lista (cabeçalhos clicáveis) ──────────────────────────
function _supIsOn(u)      { return (typeof _isOnline === 'function') && _isOnline(u.last_seen); }
function _supSeenTime(s)  { const t = s ? Date.parse(s) : NaN; return isNaN(t) ? 0 : t; }
function _supVerCmp(a, b) {
  const pa = String(a).split('.').map(n => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x - y;
  }
  return String(a).localeCompare(String(b));
}

function _supSortBy(key) {
  if (_supSort.key === key) _supSort.dir = -_supSort.dir;
  else { _supSort.key = key; _supSort.dir = 1; }
  _supPage = 0;
  _supRender();
}

function _supUpdateSortIndicators() {
  const arrow = k => (_supSort.key === k ? (_supSort.dir > 0 ? ' ▲' : ' ▼') : '');
  const set = (id, k) => { const e = document.getElementById(id); if (e) e.textContent = arrow(k); };
  set('supSortName', 'name'); set('supSortStatus', 'status');
  set('supSortSeen', 'seen'); set('supSortVer', 'ver');
}

// ── Card consolidado: distribuições por SO e por versão do agente ──────
// Calculado a partir de _supUsers (mesma lista já carregada) — sem query extra.
// Cada colaborador conta como 1 device (a máquina mais recente, vinda do LATERAL).
function _supDistHtml(counts) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return '<div class="empty-state">—</div>';
  const total = entries.reduce((s, [, n]) => s + n, 0) || 1;
  return entries.map(([k, n]) => {
    const pct = Math.round(n / total * 100);
    return `<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border)">
      <span style="flex:1;font-size:13px">${escHtml(k)}</span>
      <span style="font-size:13px;font-weight:600">${n}</span>
      <span style="color:var(--text-muted);font-size:11px;width:40px;text-align:right">${pct}%</span>
      <span style="flex-basis:110px;height:6px;background:var(--bg-2);border-radius:4px;overflow:hidden">
        <span style="display:block;height:100%;width:${pct}%;background:var(--accent,#6c8cff)"></span></span>
    </div>`;
  }).join('');
}

function _supRenderDist() {
  const so = {}, ver = {};
  for (const u of _supUsers) {
    const os = u.os || '—';
    so[os] = (so[os] || 0) + 1;
    const v = u.agent_version || '—';
    ver[v] = (ver[v] || 0) + 1;
  }
  const soBox  = document.getElementById('supSoDist');
  const verBox = document.getElementById('supVerDist');
  if (soBox)  soBox.innerHTML  = _supDistHtml(so);
  if (verBox) verBox.innerHTML = _supDistHtml(ver);
  const n = _supUsers.length;
  const st = document.getElementById('supSoTotal');  if (st) st.textContent = n + ' devices';
  const vt = document.getElementById('supVerTotal'); if (vt) vt.textContent = n + ' devices';
}

// ── Download do agente (VERSÃO ATUAL) — para o time de Suporte ──────────
// Pega o build is_latest por SO (prefere o do tenant; senão genérico) e mostra
// botões com URL pré-assinada gerada na hora (cada carga = link fresco, ~1h).
async function _supLoadAgentDownload() {
  const box = document.getElementById('supAgentDownload');
  if (!box) return;
  try {
    const d = await api('/v1/support/agent-download?tenant=' + encodeURIComponent(state.tenant));
    const items = (d && d.available) ? (d.items || []) : [];
    if (!items.length) {
      box.innerHTML = '<span style="font-size:12px;color:var(--text-muted)">Nenhum build marcado como “versão atual” ainda. Suba e promova um no Platform Admin.</span>';
      return;
    }
    const osLabel = { windows: 'Windows', linux: 'Linux' };
    box.innerHTML = items.map(it => {
      const mb  = it.size_bytes ? (it.size_bytes / 1048576).toFixed(1) + ' MB' : '';
      const tag = it.channel === 'tenant' ? 'build do tenant' : 'genérico';
      return `<a href="${it.url}" target="_blank" rel="noopener"
          style="display:inline-flex;align-items:center;gap:8px;text-decoration:none;background:var(--accent,#6c8cff);color:#fff;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:600;margin:0 8px 8px 0">
          ⬇ ${osLabel[it.os] || it.os} <span style="font-weight:400;opacity:.85">v${escHtml(it.version)} · ${tag}${mb ? ' · ' + mb : ''}</span></a>`;
    }).join('');
  } catch (e) {
    box.innerHTML = '<span style="font-size:12px;color:var(--danger,#f87171)">Falha ao carregar downloads.</span>';
  }
}
