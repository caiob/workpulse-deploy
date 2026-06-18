// =====================================================================
// wp-consolidado.js — Aba "Consolidado": toda a coleta da pessoa
// (apps + internet + AFK) AGREGADA NO SERVIDOR (o período inteiro,
// sem limite de eventos). Abre vazio; carrega ao escolher a pessoa.
// =====================================================================

let _consShowAll = false;
let _consItems   = [];   // itens agregados vindos do servidor (cache p/ filtros)

function openConsolidado()    { _consShowAll = false; loadConsolidado(); }
function applyConsolidado()   { _consShowAll = false; loadConsolidado(); }
function loadConsolidadoAll() { _consShowAll = true;  const s = document.getElementById('consUser'); if (s) s.value = ''; loadConsolidado(); }

function _renderConsPrompt() {
  ['kpiConsTotal', 'kpiConsProd', 'kpiConsUnprod', 'kpiConsUncat'].forEach(id => {
    const e = document.getElementById(id); if (e) e.innerHTML = '—';
  });
  if (state.charts.consDaily) { state.charts.consDaily.destroy(); state.charts.consDaily = null; }
  const lbl = document.getElementById('consCountLabel'); if (lbl) lbl.textContent = '';
  _consItems = [];
  const tbody = document.querySelector('#consTable tbody');
  if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="empty-state">
      <h4>Selecione um colaborador</h4>
      Escolha alguém no filtro <strong>Usuário</strong> para ver toda a coleta dele no período.
      <div style="margin-top:10px"><button class="ghost" onclick="loadConsolidadoAll()">Ver todos (pode demorar)</button></div>
    </td></tr>`;
}

async function loadConsolidado() {
  await _populateUserDropdown('consUser');
  if (typeof _applyDeepLinkUser === 'function') _applyDeepLinkUser('consUser');
  const user = document.getElementById('consUser').value.trim();
  if (!user && !_consShowAll) { _renderConsPrompt(); return; }

  const { since, until } = wpDP.getRange('consDays');
  const bucket = document.getElementById('consBucket').value;   // '' | 'afk' | 'all'
  const params = new URLSearchParams({ tenant: state.tenant, since, until });
  if (user)   params.set('user_id', user);
  if (bucket) params.set('bucket', bucket);

  try {
    const d = await api('/v1/productivity/consolidado?' + params.toString());
    const t = d.totals || {};
    const total = t.total || 0;
    const pctOf = v => total > 0 ? ` <small>${(v / total * 100).toFixed(0)}%</small>` : '';
    document.getElementById('kpiConsTotal').innerHTML  = fmtSec(total);
    document.getElementById('kpiConsProd').innerHTML   = fmtSec(t.productive   || 0) + pctOf(t.productive   || 0);
    document.getElementById('kpiConsUnprod').innerHTML = fmtSec(t.unproductive || 0) + pctOf(t.unproductive || 0);
    // 4º card: AFK quando há tempo ocioso no resultado; senão "Não categorizado".
    const afk  = t.afk || 0;
    const lbl4 = document.getElementById('kpiConsUncatLabel');
    if (afk > 0) {
      document.getElementById('kpiConsUncat').innerHTML = fmtSec(afk) + pctOf(afk);
      if (lbl4) lbl4.textContent = 'AFK (ocioso)';
    } else {
      document.getElementById('kpiConsUncat').innerHTML = fmtSec(t.uncategorized || 0) + pctOf(t.uncategorized || 0);
      if (lbl4) lbl4.textContent = 'Não categorizado';
    }

    drawConsDailyChart(d.daily || []);
    _consItems = d.items || [];
    _renderConsItems();
  } catch (e) { toast('Falha ao carregar consolidado: ' + e.message); }
}

function _renderConsItems() {
  const tbody = document.querySelector('#consTable tbody');
  if (!tbody) return;
  const cat  = document.getElementById('consCategory')?.value || '';
  const mode = document.getElementById('consGroupMode')?.value || 'detail';

  let items = cat ? _consItems.filter(i => i.category === cat) : _consItems.slice();

  if (mode === 'byapp') {
    const m = {};
    items.forEach(i => {
      const k = (i.app || '') + '|' + i.type;
      const e = m[k] || (m[k] = { app: i.app, type: i.type, total_sec: 0, category: i.category, last_access: i.last_access });
      e.total_sec += i.total_sec || 0;
      if ((i.last_access || '') > (e.last_access || '')) e.last_access = i.last_access;
    });
    items = Object.values(m).sort((a, b) => b.total_sec - a.total_sec);
  }

  const lbl = document.getElementById('consCountLabel');
  if (lbl) lbl.textContent = `— ${items.length} ${mode === 'byapp' ? 'apps/sites' : 'itens'}`;

  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Nenhuma coleta neste período/filtro.</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(i => {
    const when     = i.last_access ? ptDateTime(i.last_access) : '—';
    const dur      = fmtSec(i.total_sec || 0);
    const app      = escHtml(i.app || '—');
    const title    = (mode === 'byapp') ? '' : escHtml(i.title || '');
    const tipoColor = i.type === 'AFK' ? 'var(--warn)' : (i.type === 'Internet' ? '#6c8cff' : 'var(--text)');
    const catCell  = i.category === 'afk'
      ? '<span style="color:var(--text-dim);font-size:11px">—</span>'
      : (CAT_PILLS[i.category] || CAT_PILLS.uncategorized);
    return `<tr>
      <td style="font-size:12px;white-space:nowrap">${when}</td>
      <td style="white-space:nowrap">${dur}</td>
      <td><strong style="font-size:12px">${app}</strong>${title ? `<br><span style="color:var(--text-dim);font-size:11px">${title}</span>` : ''}</td>
      <td style="font-size:12px;color:${tipoColor};font-weight:600">${i.type}</td>
      <td>${catCell}</td>
    </tr>`;
  }).join('');
}

function drawConsDailyChart(daily) {
  if (state.charts.consDaily) state.charts.consDaily.destroy();
  if (!daily.length) return;
  const ct  = chartTheme();
  const ctx = document.getElementById('chartConsDaily').getContext('2d');
  const hasAfk = daily.some(d => (d.afk || 0) > 0);
  const ds = [
    { label: 'Produtivo',        data: daily.map(d => +(d.productive    / 3600).toFixed(2)), backgroundColor: CAT_COLORS.productive,    borderRadius: 4, stack: 'a' },
    { label: 'Não-produtivo',    data: daily.map(d => +(d.unproductive  / 3600).toFixed(2)), backgroundColor: CAT_COLORS.unproductive,  borderRadius: 4, stack: 'a' },
    { label: 'Neutro',           data: daily.map(d => +(d.neutral       / 3600).toFixed(2)), backgroundColor: CAT_COLORS.neutral,        borderRadius: 4, stack: 'a' },
    { label: 'Não categorizado', data: daily.map(d => +(d.uncategorized / 3600).toFixed(2)), backgroundColor: CAT_COLORS.uncategorized,  borderRadius: 4, stack: 'a' },
  ];
  if (hasAfk) ds.push({ label: 'AFK', data: daily.map(d => +((d.afk || 0) / 3600).toFixed(2)), backgroundColor: '#f59e0b', borderRadius: 4, stack: 'a' });
  state.charts.consDaily = new Chart(ctx, {
    type: 'bar',
    data: { labels: daily.map(d => ptDate(d.day + 'T12:00:00')), datasets: ds },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend:  { labels: { color: ct.label, font: { size: 11 }, boxWidth: 12 } },
        tooltip: { callbacks: { label: c => ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(1) + 'h' } },
      },
      scales: {
        x: { stacked: true, ticks: { color: ct.label }, grid: { color: ct.grid } },
        y: { stacked: true, ticks: { color: ct.label, callback: v => v + 'h' }, grid: { color: ct.grid } },
      },
    },
  });
}
