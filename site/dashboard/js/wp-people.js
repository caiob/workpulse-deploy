// =====================================================================
// wp-people.js — Aba "Pessoas" (ranking + drill-down individual)
// =====================================================================

async function loadPeople() {
  try {
    const days = +document.getElementById('peopleRange').value;
    const data = await api(`/v1/team/summary?tenant=${encodeURIComponent(state.tenant)}&days=${days}`);
    state.people = data.users || [];
    renderPeopleTable();
  } catch (e) { toast('Erro ao carregar pessoas: ' + e.message); }
}

function renderPeopleTable() {
  const q    = (document.getElementById('peopleFilter').value || '').toLowerCase();
  const rows = state.people.filter(u => {
    const dn = displayName(u).toLowerCase();
    return !q || dn.includes(q)
       || (u.user_id    || '').toLowerCase().includes(q)
       || (u.email      || '').toLowerCase().includes(q)
       || (u.department || '').toLowerCase().includes(q);
  });
  const tb = document.querySelector('#peopleTable tbody');
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="7" class="empty-state">Nenhum colaborador no período.</td></tr>`;
    return;
  }
  tb.innerHTML = rows.map(u => {
    const total = (u.active || 0) + (u.afk || 0);
    const adh   = total > 0 ? Math.round((u.active || 0) / total * 100) : 0;
    const name  = displayName(u);
    const safeUid  = (u.user_id || '').replace(/'/g, "\\'");
    const safeName = (name || '').replace(/'/g, "\\'");
    return `<tr>
      <td>${avatar(name)} <strong>${name}</strong></td>
      <td>${u.department || '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${fmtH(u.active)}<small style="color:var(--text-dim)"> h</small></td>
      <td>${fmtH(u.afk)}<small style="color:var(--text-dim)"> h</small></td>
      <td><span class="bar"><span style="width:${adh}%"></span></span>
          <small style="color:var(--text-dim);margin-left:6px">${adh}%</small></td>
      <td>${relTime(u.last_seen)}</td>
      <td style="display:flex;gap:6px">
        <button class="ghost" onclick="drillUser('${safeUid}','${safeName}')">abrir</button>
        <button class="cat-btn" onclick="editUser('${safeUid}')">✏ Editar</button>
      </td>
    </tr>`;
  }).join('');
}

async function drillUser(uid, name) {
  try {
    const data  = await api(`/v1/users/${encodeURIComponent(uid)}/summary?tenant=${encodeURIComponent(state.tenant)}&days=30`);
    const total = (data.active_seconds || 0) + (data.afk_seconds || 0);
    const adh   = total > 0 ? Math.round((data.active_seconds || 0) / total * 100) : 0;
    const ct    = chartTheme();

    const drill = document.getElementById('drill');
    drill.classList.remove('hide');
    drill.className = 'drill';
    drill.innerHTML = `
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:10px">
        ${avatar(name, true)}
        <div>
          <h4>${name}</h4>
          <div class="meta">${uid} · últimos 30 dias</div>
        </div>
      </div>
      <div class="stat-row">
        <div><h3>Ativo</h3><div class="stat">${fmtH(data.active_seconds)}<small>h</small></div></div>
        <div><h3>AFK</h3><div class="stat">${fmtH(data.afk_seconds)}<small>h</small></div></div>
        <div><h3>Aderência</h3><div class="stat">${adh}<small>%</small></div></div>
        <div><h3>Dias com atividade</h3><div class="stat">${(data.by_day || []).length}<small>dias</small></div></div>
      </div>
      <div style="height:240px;margin-bottom:16px"><canvas id="drillChart"></canvas></div>
      <h3>Top aplicativos no período</h3>
      <table style="margin-top:8px">
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
          }).join('') || `<tr><td colspan="3" class="empty-state">Sem dados</td></tr>`}
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
