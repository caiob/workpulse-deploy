// =====================================================================
// wp-internet.js — Aba "Internet" (análise de navegação web)
// =====================================================================

async function loadInternet() {
  await _populateUserDropdown('intUser');
  const days   = document.getElementById('intDays').value;
  const user   = document.getElementById('intUser').value.trim();
  const params = new URLSearchParams({ tenant: state.tenant, days });
  if (user) params.set('user_id', user);

  try {
    const data  = await api('/v1/productivity/internet?' + params.toString());
    const t     = data.totals || {};
    const total = t.total || 0;
    const pctOf = v => total > 0 ? ` <small>${(v / total * 100).toFixed(0)}%</small>` : '';

    document.getElementById('kpiIntTotal').innerHTML  = fmtSec(total);
    document.getElementById('kpiIntProd').innerHTML   = fmtSec(t.productive   || 0) + pctOf(t.productive   || 0);
    document.getElementById('kpiIntUnprod').innerHTML = fmtSec(t.unproductive || 0) + pctOf(t.unproductive || 0);
    document.getElementById('kpiIntUncat').innerHTML  = fmtSec(t.uncategorized || 0) + pctOf(t.uncategorized || 0);

    drawIntDailyChart(data.daily || []);
    renderSitesTable(data.sites || [], total);
  } catch (e) { toast('Erro ao carregar dados de internet: ' + e.message); }
}

function drawIntDailyChart(daily) {
  if (state.charts.intDaily) state.charts.intDaily.destroy();
  if (!daily.length) return;
  const ct  = chartTheme();
  const ctx = document.getElementById('chartIntDaily').getContext('2d');
  state.charts.intDaily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => ptDate(d.day + 'T00:00:00Z')),
      datasets: [
        { label: 'Produtivo',        data: daily.map(d => +(d.productive    / 3600).toFixed(2)), backgroundColor: CAT_COLORS.productive,    borderRadius: 4, stack: 'a' },
        { label: 'Não-produtivo',    data: daily.map(d => +(d.unproductive  / 3600).toFixed(2)), backgroundColor: CAT_COLORS.unproductive,  borderRadius: 4, stack: 'a' },
        { label: 'Neutro',           data: daily.map(d => +(d.neutral       / 3600).toFixed(2)), backgroundColor: CAT_COLORS.neutral,        borderRadius: 4, stack: 'a' },
        { label: 'Não categorizado', data: daily.map(d => +(d.uncategorized / 3600).toFixed(2)), backgroundColor: CAT_COLORS.uncategorized,  borderRadius: 4, stack: 'a' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: ct.label, font: { size: 11 }, boxWidth: 12 } },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(1) + 'h' } }
      },
      scales: {
        x: { stacked: true, ticks: { color: ct.label }, grid: { color: ct.grid } },
        y: { stacked: true, ticks: { color: ct.label, callback: v => v + 'h' }, grid: { color: ct.grid } }
      }
    }
  });
}

function renderSitesTable(sites, totalSec) {
  const tbody = document.querySelector('#intSitesTable tbody');
  if (!sites.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">
      <h4>Nenhum dado de browser encontrado.</h4>
      Verifique se há eventos do tipo <code>window</code> com app de browser (msedge.exe, chrome.exe, firefox.exe...).
    </td></tr>`;
    return;
  }
  tbody.innerHTML = sites.slice(0, 150).map(s => {
    const pct     = totalSec > 0 ? (s.total_sec / totalSec * 100).toFixed(1) : '0';
    const barFill = CAT_COLORS[s.category] || CAT_COLORS.uncategorized;
    const bar     = `<div class="bar"><span style="width:${Math.min(+pct, 100)}%;background:${barFill}"></span></div>`;
    const pill    = CAT_PILLS[s.category] || CAT_PILLS.uncategorized;
    const users   = s.user_count === 1 ? '1 usuário' : s.user_count + ' usuários';
    const safeName = s.site_name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    return `<tr>
      <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${s.site_name}">${s.site_name}</td>
      <td>${pill}</td>
      <td>${fmtSec(s.total_sec)}</td>
      <td>${bar} <span style="color:var(--text-dim);font-size:11px">${pct}%</span></td>
      <td style="color:var(--text-dim)">${users}</td>
      <td><button class="cat-btn" onclick="quickRule('${safeName}')">Categorizar</button></td>
    </tr>`;
  }).join('');
}
