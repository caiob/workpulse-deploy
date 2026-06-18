// =====================================================================
// wp-people.js — Aba "Pessoas" (ranking + drill-down individual)
// =====================================================================

/** True se o agente reportou dentro da janela de "online". */
function _isOnline(lastSeen) {
  if (!lastSeen) return false;
  const ms = Date.now() - new Date(lastSeen).getTime();
  return ms >= 0 && ms < (WP_ONLINE_MINUTES * 60000);
}

/** Vem do clique no card "Online agora": abre Pessoas já filtrado por online. */
function goOnlinePeople() {
  const sel = document.getElementById('peopleActivity');
  if (sel) sel.value = 'online';
  switchTab('people');   // dispara loadPeople() -> renderPeopleTable() com o filtro aplicado
}

async function loadPeople() {
  try {
    const { since, until } = wpDP.getRange('peopleRange');
    const data = await api(`/v1/team/summary?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`);
    state.people = data.users || [];
    _peoplePage = 0;
    renderPeopleTable();
  } catch (e) { toast('Erro ao carregar pessoas: ' + e.message); }
}

let _peoplePerPage = 10;
let _peoplePage = 0;
let _peopleSort = { key: 'active', dir: 'desc' };   // default: mais ativos primeiro

function peopleApplyFilter() { _peoplePage = 0; renderPeopleTable(); }

function peopleSort(key) {
  if (_peopleSort.key === key) {
    _peopleSort.dir = _peopleSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    _peopleSort.key = key;
    _peopleSort.dir = (key === 'name') ? 'asc' : 'desc';   // nome A→Z; números maior→menor
  }
  _peoplePage = 0;
  renderPeopleTable();
}

function peoplePage(d) { _peoplePage += d; renderPeopleTable(); }

function peopleSetPageSize(n) { _peoplePerPage = parseInt(n, 10) || 10; _peoplePage = 0; renderPeopleTable(); }

function _peopleAdh(u) {
  const t = (u.active || 0) + (u.afk || 0);
  return t > 0 ? (u.active || 0) / t : 0;
}

function _peopleCmp(a, b) {
  const { key, dir } = _peopleSort;
  const mul = dir === 'asc' ? 1 : -1;
  if (key === 'name') {
    const av = displayName(a).toLowerCase(), bv = displayName(b).toLowerCase();
    return (av < bv ? -1 : av > bv ? 1 : 0) * mul;
  }
  const f = key === 'afk' ? (u => u.afk || 0)
          : key === 'adh' ? _peopleAdh
          : (u => u.active || 0);
  return (f(a) - f(b)) * mul;
}

function _peopleUpdateArrows() {
  document.querySelectorAll('#peopleTable .sort-ar').forEach(el => {
    el.textContent = (el.dataset.k === _peopleSort.key)
      ? (_peopleSort.dir === 'asc' ? ' ▲' : ' ▼') : '';
  });
}

function _peopleRenderPager(total, start, shown) {
  const box = document.getElementById('peoplePagination');
  if (!box) return;
  const pages = Math.ceil(total / _peoplePerPage);
  const sizeSel = `<label style="font-size:12px;color:var(--text-dim)">Por página
    <select class="card-sel" onchange="peopleSetPageSize(this.value)" style="margin-left:6px">
      ${[10, 25, 50, 100].map(n => `<option value="${n}" ${n === _peoplePerPage ? 'selected' : ''}>${n}</option>`).join('')}
    </select></label>`;
  const nav = pages > 1
    ? `<button class="ghost" ${_peoplePage === 0 ? 'disabled' : ''} onclick="peoplePage(-1)">‹ Anterior</button>
       <span style="color:var(--text-dim)">${start + 1}–${start + shown} de ${total}</span>
       <button class="ghost" ${_peoplePage >= pages - 1 ? 'disabled' : ''} onclick="peoplePage(1)">Próxima ›</button>`
    : `<span style="color:var(--text-dim)">${total} colaborador${total === 1 ? '' : 'es'}</span>`;
  box.innerHTML = `<div style="display:flex;align-items:center;gap:14px;justify-content:flex-end;margin-top:10px;font-size:13px;flex-wrap:wrap">
    ${sizeSel}${nav}
  </div>`;
}

function renderPeopleTable() {
  const q   = (document.getElementById('peopleFilter').value || '').toLowerCase();
  const act = (document.getElementById('peopleActivity') || {}).value || 'all';
  const rows = state.people.filter(u => {
    const dn = displayName(u).toLowerCase();
    const matchText = !q || dn.includes(q)
       || (u.user_id    || '').toLowerCase().includes(q)
       || (u.email      || '').toLowerCase().includes(q)
       || (u.department || '').toLowerCase().includes(q);
    if (!matchText) return false;
    if (act === 'online'  && !_isOnline(u.last_seen)) return false;
    if (act === 'offline' &&  _isOnline(u.last_seen)) return false;
    return true;
  });
  rows.sort(_peopleCmp);
  _peopleUpdateArrows();

  const tb = document.querySelector('#peopleTable tbody');
  const totalRows = rows.length;
  if (!totalRows) {
    const msg = act === 'online' ? 'Ninguém online agora.' : 'Nenhum colaborador no período.';
    tb.innerHTML = `<tr><td colspan="9" class="empty-state">${msg}</td></tr>`;
    const box = document.getElementById('peoplePagination'); if (box) box.innerHTML = '';
    return;
  }
  const pages = Math.ceil(totalRows / _peoplePerPage);
  if (_peoplePage >= pages) _peoplePage = pages - 1;
  if (_peoplePage < 0) _peoplePage = 0;
  const start = _peoplePage * _peoplePerPage;
  const pageRows = rows.slice(start, start + _peoplePerPage);

  tb.innerHTML = pageRows.map(u => {
    const total = (u.active || 0) + (u.afk || 0);
    const adh   = total > 0 ? Math.round((u.active || 0) / total * 100) : 0;
    const name  = displayName(u);
    const safeUid  = (u.user_id || '').replace(/'/g, "\\'");
    const safeName = (name || '').replace(/'/g, "\\'");
    return `<tr>
      <td><span class="person-link" title="Atalhos desta pessoa" onclick="openPersonMenu(event,'${safeUid}','${safeName}')">${avatar(name)} <strong>${name}</strong></span></td>
      <td>${u.department || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${fmtH(u.active)}<small style="color:var(--text-dim)"> h</small></td>
      <td>${fmtH(u.afk)}<small style="color:var(--text-dim)"> h</small></td>
      <td><span class="bar"><span style="width:${adh}%"></span></span>
          <small style="color:var(--text-dim);margin-left:6px">${adh}%</small></td>
      <td>${_isOnline(u.last_seen)
        ? '<span style="display:inline-flex;align-items:center;gap:5px;color:#4ade80"><span style="width:8px;height:8px;border-radius:50%;background:#4ade80;display:inline-block"></span>Online</span>'
        : '<span style="display:inline-flex;align-items:center;gap:5px;color:var(--text-muted)"><span style="width:8px;height:8px;border-radius:50%;background:var(--text-muted);display:inline-block"></span>Offline</span>'}</td>
      <td>${relTime(u.last_seen)}</td>
      <td><code style="font-size:11px;color:var(--text-dim)">${u.agent_version || '—'}</code></td>
      <td style="display:flex;gap:6px">
        <button class="ghost" onclick="drillUser('${safeUid}','${safeName}')">abrir</button>
        <button class="cat-btn" onclick="editUser('${safeUid}')">✏ Editar</button>
      </td>
    </tr>`;
  }).join('');
  _peopleRenderPager(totalRows, start, pageRows.length);
}

/* ── Menu de atalhos por pessoa (abre ao clicar no nome/avatar) ───────────── */
function _closePersonMenu() { document.getElementById('personMenu')?.remove(); }

function openPersonMenu(ev, uid, name) {
  ev.stopPropagation();
  _closePersonMenu();
  const su = (uid || '').replace(/'/g, "\\'");
  const sn = (name || '').replace(/'/g, "\\'");
  const m = document.createElement('div');
  m.id = 'personMenu';
  m.className = 'person-menu';
  m.innerHTML = `
    <div class="pm-head">${escHtml(name)}</div>
    <button onclick="goToUserPage('events','${su}','${sn}')">⚡ Aplicativos</button>
    <button onclick="goToUserPage('internet','${su}','${sn}')">🌐 Internet</button>
    <button onclick="goToUserPage('jornada','${su}','${sn}')">⏱ Jornada de Trabalho</button>
    <button onclick="goToUserPage('localizacao','${su}','${sn}')">📍 Localização</button>
    <button onclick="_closePersonMenu();drillUser('${su}','${sn}')">🔎 Desdobramento (aqui)</button>`;
  document.body.appendChild(m);
  const x = Math.min(ev.clientX, window.innerWidth  - 240);
  const y = Math.min(ev.clientY, window.innerHeight - 260);
  m.style.left = Math.max(8, x) + 'px';
  m.style.top  = Math.max(8, y) + 'px';
  setTimeout(() => document.addEventListener('click', _closePersonMenu, { once: true }), 0);
}

function goToUserPage(page, uid, name) {
  _closePersonMenu();
  _deepLinkUser = { uid, name };
  if (typeof switchTab === 'function') switchTab(page);
}

/** Mapeia o valor do seletor para um rótulo legível */
function _peoplePeriodLabel(days) {
  const d = +days;
  if (d === 1)  return 'Hoje';
  if (d === 7)  return 'Últimos 7 dias';
  if (d === 14) return 'Últimas 2 semanas';
  if (d === 30) return 'Últimos 30 dias';
  return `Últimos ${d} dias`;
}

async function drillUser(uid, name) {
  try {
    const { since, until } = wpDP.getRange('peopleRange');
    const label = _peoplePeriodLabel(wpDP.getDays('peopleRange'));
    const data  = await api(`/v1/users/${encodeURIComponent(uid)}/summary?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`);
    const total = (data.active_seconds || 0) + (data.afk_seconds || 0);
    const adh   = total > 0 ? Math.round((data.active_seconds || 0) / total * 100) : 0;
    const ct    = chartTheme();

    const drill = document.getElementById('drill');
    drill.classList.remove('hide');
    drill.className = 'drill';
    drill.innerHTML = `
      <!-- ── Cabeçalho da pessoa ── -->
      <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px">
        <div style="display:flex;align-items:center;gap:14px">
          ${avatar(name, true)}
          <div>
            <h4 style="margin:0 0 2px">${name}</h4>
            <div class="meta" style="font-size:11px">${uid}</div>
          </div>
        </div>
        <button class="ghost" title="Fechar"
          onclick="document.getElementById('drill').classList.add('hide')"
          style="font-size:20px;line-height:1;padding:2px 10px;opacity:.6">×</button>
      </div>

      <!-- ── KPIs com rótulo de período explícito ── -->
      <div style="margin-bottom:22px">
        <div style="font-size:11px;font-weight:600;letter-spacing:.07em;color:var(--text-muted);text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          Resumo — <span style="color:var(--primary)">${label}</span>
        </div>
        <div class="stat-row">
          <div><h3>Ativo</h3><div class="stat">${fmtH(data.active_seconds)}<small>h</small></div></div>
          <div><h3>AFK</h3><div class="stat">${fmtH(data.afk_seconds)}<small>h</small></div></div>
          <div><h3>Aderência</h3><div class="stat">${adh}<small>%</small></div></div>
          <div><h3>Dias c/ atividade</h3><div class="stat">${(data.by_day || []).length}<small>dias</small></div></div>
        </div>
      </div>

      <!-- ── Gráfico de curva ── -->
      <div style="margin-bottom:20px">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px">
          Ativo × AFK — dia a dia <span style="color:var(--text-muted);font-size:11px">(${label})</span>
        </div>
        <div style="height:240px"><canvas id="drillChart"></canvas></div>
      </div>

      <!-- ── Top apps ── -->
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px">
        Top aplicativos <span style="color:var(--text-muted);font-size:11px">(${label})</span>
      </div>
      <table style="margin-top:0">
        <thead><tr><th>Aplicativo</th><th>Tempo</th><th>Distribuição</th></tr></thead>
        <tbody>
          ${(data.top_apps || []).map(a => {
            const pct = total > 0 ? Math.round((a.seconds / total) * 100) : 0;
            return `<tr>
              <td><strong>${a.app || '—'}</strong></td>
              <td>${fmtSec(a.seconds)}</td>
              <td><span class="bar" style="width:200px"><span style="width:${Math.min(100, pct)}%"></span></span>
                  <small style="color:var(--text-dim);margin-left:6px">${pct}%</small></td>
            </tr>`;
          }).join('') || `<tr><td colspan="3" class="empty-state">Sem dados no período.</td></tr>`}
        </tbody>
      </table>
    `;

    if (state.charts.drill) state.charts.drill.destroy();
    state.charts.drill = new Chart(document.getElementById('drillChart').getContext('2d'), {
      type: 'line',
      data: {
        labels: (data.by_day || []).map(d => ptDate(d.day)),
        datasets: [
          { label: 'Ativo (h)', data: (data.by_day || []).map(d => +(d.active / 3600).toFixed(2)),
            borderColor: '#6c8cff', backgroundColor: 'rgba(108,140,255,.18)',
            fill: true, tension: 0.3, pointRadius: 3 },
          { label: 'AFK (h)', data: (data.by_day || []).map(d => +(d.afk / 3600).toFixed(2)),
            borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,.12)',
            fill: true, tension: 0.3, pointRadius: 3 },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: ct.legend } } },
        scales: {
          x: { ticks: { color: ct.label }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { color: ct.label }, grid: { color: ct.grid } }
        }
      }
    });

    drill.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { toast('Erro: ' + e.message); }
}
