// =====================================================================
// wp-analytics.js — Abas "Alertas" e "Tendências"
// =====================================================================

/* -------------------- ALERTAS -------------------- */
async function loadAlerts() {
  try {
    const days = +(document.getElementById('rangeDays')?.value || 7);
    const data = state.lastOverview ||
      await api(`/v1/workforce/overview?tenant=${encodeURIComponent(state.tenant)}&days=${days}`);
    const over  = data.alerts.overwork  || [];
    const under = data.alerts.underwork || [];

    document.getElementById('overworkList').innerHTML = over.length
      ? over.map(p => `
          <div class="alert-tile over">
            <div class="icon">!</div>
            <div class="body">
              <div class="name">${p.display_name || p.user_id}</div>
              <div class="meta">Pico de <strong>${p.max_day_hours}h</strong> ativos em um único dia.</div>
            </div>
          </div>`).join('')
      : `<div class="alert-tile empty">Nenhum sinal de sobrecarga no período.</div>`;

    document.getElementById('underworkList').innerHTML = under.length
      ? under.map(p => `
          <div class="alert-tile under">
            <div class="icon">~</div>
            <div class="body">
              <div class="name">${p.display_name || p.user_id}</div>
              <div class="meta">Média diária de <strong>${p.avg_day_hours}h</strong> ativas.</div>
            </div>
          </div>`).join('')
      : `<div class="alert-tile empty">Nenhum sinal de baixo engajamento no período.</div>`;
  } catch (e) { toast('Erro: ' + e.message); }
}

/* -------------------- TENDÊNCIAS -------------------- */
async function loadTrends() {
  try {
    const days = +document.getElementById('trendDays').value;
    const [active, afk] = await Promise.all([
      api(`/v1/workforce/timeseries?tenant=${encodeURIComponent(state.tenant)}&days=${days}&bucket=window`),
      api(`/v1/workforce/timeseries?tenant=${encodeURIComponent(state.tenant)}&days=${days}&bucket=afk`),
    ]);
    drawTrend('chartTrendActive', active.series, '#6c8cff', 'Tempo ativo');
    drawTrend('chartTrendAfk',    afk.series,    '#f59e0b', 'AFK');
  } catch (e) { toast('Erro nas tendências: ' + e.message); }
}

function drawTrend(canvasId, series, color, label) {
  const ct = chartTheme();
  if (state.charts[canvasId]) state.charts[canvasId].destroy();
  state.charts[canvasId] = new Chart(document.getElementById(canvasId).getContext('2d'), {
    type: 'line',
    data: {
      labels: series.map(s => ptDate(s.day)),
      datasets: [{
        label:           label + ' (h)',
        data:            series.map(s => +(s.seconds / 3600).toFixed(2)),
        borderColor:     color,
        backgroundColor: color + '22',
        fill:            true,
        tension:         0.35,
        pointRadius:     3,
        pointBackgroundColor: color,
      }]
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
}
