// =====================================================================
// wp-overview.js — Aba "Visão Geral"
// =====================================================================

let _autoRefreshTimer = null;

function setAutoRefresh() {
  if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
  const secs = +document.getElementById('autoRefreshSel').value;
  if (secs > 0) _autoRefreshTimer = setInterval(() => loadOverview(), secs * 1000);
}

async function loadOverview() {
  const { since, until } = wpDP.getRange('rangeDays');
  const qs = `tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`;

  // Feedback imediato: marca os KPIs como "carregando" enquanto as queries rodam.
  ['kpiOnline', 'kpiActive', 'kpiToday', 'kpiAdh'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '<span style="opacity:.4">···</span>';
  });
  _setConnectionStatus('carregando', false);

  // As duas chamadas seguem em paralelo, mas cada bloco PINTA assim que chega
  // a sua resposta — quem volta primeiro não fica esperando o outro.
  const pOverview = api(`/v1/workforce/overview?${qs}`)
    .then(data => {
      state.lastOverview = data;
      const t = data.totals;
      document.getElementById('kpiOnline').innerHTML    = `${t.online_now || 0}<small>pessoas</small>`;
      document.getElementById('kpiOnlineSub').textContent = `${t.total_users || 0} cadastrados no período`;
      document.getElementById('kpiActive').innerHTML    = `${fmtH(t.active_seconds)}<small>h</small>`;
      document.getElementById('kpiActiveSub').textContent = `equivalente a ${Math.round((t.active_seconds || 0) / 3600 / (t.total_users || 1))}h por pessoa`;
      document.getElementById('kpiToday').innerHTML     = `${fmtH(t.today_active_seconds)}<small>h</small>`;
      const sumTotal = (t.active_seconds || 0) + (t.afk_seconds || 0);
      const adh      = sumTotal > 0 ? (t.active_seconds / sumTotal * 100) : 0;
      document.getElementById('kpiAdh').innerHTML       = `${adh.toFixed(0)}<small>%</small>`;
      drawDailyChart(data.daily || []);
      drawDepartments(data.by_department || []);
      drawHeatmap(data.hourly || [], data.daily || []);
    })
    .catch(e => { _setConnectionStatus('erro', true); toast('Falha ao carregar visão geral: ' + e.message); });

  const pTeam = api(`/v1/team/summary?${qs}`)
    .then(team => {
      state.teamUsers = team.users || [];
      drawUsersChart();
      drawAppsChart(team.top_apps || []);
    })
    .catch(e => { toast('Falha ao carregar equipe: ' + e.message); });

  await Promise.allSettled([pOverview, pTeam]);
  _setConnectionStatus('conectado', false);
  const hhmm = new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  const lbl  = document.getElementById('lastRefreshLabel');
  if (lbl) lbl.textContent = `Atualizado às ${hhmm}`;
}

function drawDailyChart(daily) {
  if (state.charts.daily) state.charts.daily.destroy();
  const ct = chartTheme();
  const ctx = document.getElementById('chartDaily').getContext('2d');
  state.charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => ptDate(d.day + 'T12:00:00')),
      datasets: [
        { label: 'Ativo (h)', data: daily.map(d => +(d.active / 3600).toFixed(2)),
          backgroundColor: 'rgba(108,140,255,.85)', borderRadius: 4, stack: 'a' },
        { label: 'AFK (h)',   data: daily.map(d => +(d.afk / 3600).toFixed(2)),
          backgroundColor: 'rgba(245,158,11,.6)',  borderRadius: 4, stack: 'a' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: ct.legend, boxWidth: 12 } } },
      scales: {
        x: { stacked: true, ticks: { color: ct.label }, grid: { display: false } },
        y: { stacked: true, beginAtZero: true, ticks: { color: ct.label }, grid: { color: ct.grid } },
      }
    }
  });
}

function drawDepartments(depts) {
  const total = depts.reduce((a, d) => a + (d.active || 0), 0) || 1;
  const html  = depts.length === 0
    ? '<div class="empty-state"><h4>Sem dados de departamento</h4>Configure <code>department</code> nos perfis dos colaboradores.</div>'
    : depts.map(d => {
        const pct = Math.round((d.active || 0) / total * 100);
        return `<div class="dept-row">
          <div>
            <div class="name">${d.dept}</div>
            <div class="meta">${d.users} pessoa${d.users === 1 ? '' : 's'}</div>
          </div>
          <div class="right">
            <div class="hours">${fmtH(d.active)}h</div>
            <div class="pct">${pct}%</div>
          </div>
          <div class="dept-bar"><span style="width:${pct}%"></span></div>
        </div>`;
      }).join('');
  document.getElementById('deptList').innerHTML = html;
}

function drawUsersChart() {
  const afkOf = u => (u.afk_in != null ? u.afk_in : u.afk) || 0;
  const mode  = (document.getElementById('usersChartSort') || {}).value || 'active';
  const sorted = (state.teamUsers || []).slice().sort(
    mode === 'afk'
      ? (a, b) => afkOf(b) - afkOf(a)
      : (a, b) => (b.active || 0) - (a.active || 0)
  );
  const top = sorted.slice(0, 12);

  const sub = document.getElementById('usersChartSub');
  if (sub) sub.textContent = mode === 'afk'
    ? 'Top 12 colaboradores com mais AFK na jornada no período. Cada barra soma tempo ativo + AFK dentro da jornada.'
    : 'Top 12 colaboradores com mais tempo ativo no período. Cada barra soma tempo ativo + AFK dentro da jornada.';

  const ct  = chartTheme();
  if (state.charts.users) state.charts.users.destroy();
  state.charts.users = new Chart(document.getElementById('chartUsers').getContext('2d'), {
    type: 'bar',
    data: {
      labels: top.map(u => displayName(u)),
      datasets: [
        { label: 'Ativo (h)', data: top.map(u => +(u.active / 3600).toFixed(2)),
          backgroundColor: 'rgba(108,140,255,.85)', borderRadius: 4, stack: 'a' },
        // AFK na Jornada (exclui AFK pós-jornada), igual à tela Jornada de Trabalho.
        { label: 'AFK na jornada (h)', data: top.map(u => +(((u.afk_in != null ? u.afk_in : u.afk) || 0) / 3600).toFixed(2)),
          backgroundColor: 'rgba(245,158,11,.6)', borderRadius: 4, stack: 'a' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: ct.legend, boxWidth: 12 } } },
      scales: {
        x: { stacked: true, ticks: { color: ct.label }, grid: { display: false } },
        y: { stacked: true, beginAtZero: true, ticks: { color: ct.label }, grid: { color: ct.grid } },
      }
    }
  });
}

function drawAppsChart(apps) {
  const list = apps || [];
  const top  = list.slice(0, 7);
  const ct   = chartTheme();
  // Total de TODOS os apps no período (denominador do %).
  const totalSec = list.reduce((s, a) => s + (a.seconds || 0), 0) || 1;
  if (state.charts.apps) state.charts.apps.destroy();
  state.charts.apps = new Chart(document.getElementById('chartApps').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: top.map(a => a.app || 'desconhecido'),
      datasets: [{
        data: top.map(a => +(a.seconds / 3600).toFixed(2)),
        backgroundColor: ['#6c8cff','#34d399','#f59e0b','#a78bfa','#f472b6','#22d3ee','#fb7185'],
        borderColor: ct.border, borderWidth: 2
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'right', labels: { color: ct.legend, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const sec = top[ctx.dataIndex] ? (top[ctx.dataIndex].seconds || 0) : 0;
              const h   = (sec / 3600).toFixed(1).replace('.', ',');
              const pct = (sec / totalSec * 100).toFixed(1).replace('.', ',');
              return ` ${ctx.label}: ${h} h (${pct}% do período)`;
            }
          }
        }
      }
    }
  });
}

function drawHeatmap(hourly, daily) {
  const byHour = {};
  (daily || []).forEach(d => {
    (d.by_hour || []).forEach(h => {
      byHour[h.hour] = (byHour[h.hour] || 0) + (h.active || 0);
    });
  });
  if (!Object.keys(byHour).length && hourly.length) {
    hourly.forEach(h => { byHour[h.hour] = (byHour[h.hour] || 0) + (h.seconds || 0); });
  }

  const maxVal = Math.max(...Object.values(byHour), 1);
  const colorFor = v => {
    const t = Math.min(v / maxVal, 1);
    return `rgba(108,140,255,${(0.08 + t * 0.92).toFixed(2)})`;
  };

  let row = `<div class="heat-row"><div class="label">Total</div>`;
  for (let h = 0; h < 24; h++) {
    const v   = byHour[h] || 0;
    const tip = `${h.toString().padStart(2, '0')}h — ${fmtH(v)}h`;
    row += `<div class="heat-cell" style="background:${colorFor(v)}" title="${tip}"></div>`;
  }
  row += `</div>`;

  let labels = `<div class="heat-row"><div class="label" style="opacity:0">.</div>`;
  for (let h = 0; h < 24; h++) {
    labels += `<div style="font-size:9px;color:var(--text-muted);text-align:center">${h % 6 === 0 ? h : ''}</div>`;
  }
  labels += `</div>`;

  document.getElementById('heatmap').innerHTML = row + labels;
}
