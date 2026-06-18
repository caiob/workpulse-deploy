// =====================================================================
// wp-consolidado.js — Aba "Consolidado": toda a coleta da pessoa
// (apps + internet + AFK) AGREGADA NO SERVIDOR (o período inteiro,
// sem limite de eventos). Abre vazio; carrega ao escolher a pessoa.
// =====================================================================

let _consShowAll  = false;
let _consItems    = [];      // itens vindos do servidor (cache p/ filtros client-side)
let _consTimeline = false;   // true quando os itens são blocos cronológicos (Sequencial)
let _consView     = [];      // lista final (filtrada+ordenada+agrupada) a paginar
let _consPage     = 0;
let _CONS_PAGE_SIZE = 50;

// HH:MM local (mesma convenção do ptDateTime do resto do dashboard)
function _consTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
}

function openConsolidado()    { _consShowAll = false; loadConsolidado(); }
function applyConsolidado()   { _consShowAll = false; loadConsolidado(); }
function loadConsolidadoAll() { _consShowAll = true;  const s = document.getElementById('consUser'); if (s) s.value = ''; loadConsolidado(); }

function _renderConsPrompt() {
  ['kpiConsTotal', 'kpiConsProd', 'kpiConsUnprod', 'kpiConsUncat'].forEach(id => {
    const e = document.getElementById(id); if (e) e.innerHTML = '—';
  });
  if (state.charts.consDaily) { state.charts.consDaily.destroy(); state.charts.consDaily = null; }
  const lbl = document.getElementById('consCountLabel'); if (lbl) lbl.textContent = '';
  const pag = document.getElementById('consPagination'); if (pag) pag.innerHTML = '';
  _consItems = []; _consView = []; _consPage = 0;
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
  const mode = document.getElementById('consGroupMode')?.value || 'detail';
  const seq  = (mode === 'sequential');   // timeline cronológica

  if (!user && !_consShowAll) { _renderConsPrompt(); return; }
  if (seq && !user) {  // a visão sequencial é por pessoa
    const pag = document.getElementById('consPagination'); if (pag) pag.innerHTML = '';
    const lbl = document.getElementById('consCountLabel'); if (lbl) lbl.textContent = '';
    const tbody = document.querySelector('#consTable tbody');
    if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="empty-state">
        <h4>Sequencial é por colaborador</h4>
        A linha do tempo mostra o que <strong>uma pessoa</strong> fez na ordem do dia.
        Selecione alguém no filtro <strong>Usuário</strong>.</td></tr>`;
    return;
  }

  const { since, until } = wpDP.getRange('consDays');
  const bucket = document.getElementById('consBucket').value;   // '' | 'afk' | 'all'
  const params = new URLSearchParams({ tenant: state.tenant, since, until });
  if (user)   params.set('user_id', user);
  if (bucket) params.set('bucket', bucket);

  const path = seq ? '/v1/productivity/timeline' : '/v1/productivity/consolidado';
  try {
    const d = await api(path + '?' + params.toString());
    _consTimeline = seq;
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

// Monta a lista final (filtro de categoria + ordenação + agrupamento) e pagina.
function _renderConsItems() {
  const cat  = document.getElementById('consCategory')?.value || '';
  const mode = document.getElementById('consGroupMode')?.value || 'detail';

  let items = cat ? _consItems.filter(i => i.category === cat) : _consItems.slice();

  if (_consTimeline) {
    // Linha do tempo: do mais novo para o mais antigo.
    items.sort((a, b) => (b.start_ts || '').localeCompare(a.start_ts || ''));
  } else if (mode === 'byapp') {
    // Colapsa por app/site; maior tempo primeiro.
    const m = {};
    items.forEach(i => {
      const k = (i.app || '') + '|' + i.type;
      const e = m[k] || (m[k] = { app: i.app, type: i.type, total_sec: 0, category: i.category, last_access: i.last_access });
      e.total_sec += i.total_sec || 0;
      if ((i.last_access || '') > (e.last_access || '')) e.last_access = i.last_access;
    });
    items = Object.values(m).sort((a, b) => b.total_sec - a.total_sec);
  }
  // 'detail' = como veio do servidor (por tempo, maior primeiro).

  _consView = items;
  _consPage = 0;
  _renderConsPage();
}

function _renderConsPage() {
  const tbody = document.querySelector('#consTable tbody');
  if (!tbody) return;
  const total = _consView.length;
  const label = _consTimeline ? 'blocos' : (document.getElementById('consGroupMode')?.value === 'byapp' ? 'apps/sites' : 'itens');

  if (!total) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Nenhuma coleta neste período/filtro.</td></tr>';
    _renderCfgPagination('consPagination', 'consCountLabel', 0, 0, _CONS_PAGE_SIZE, '_consGoTo', label);
    return;
  }

  const start = _consPage * _CONS_PAGE_SIZE;
  const slice = _consView.slice(start, start + _CONS_PAGE_SIZE);
  tbody.innerHTML = slice.map(_consRow).join('');
  _renderCfgPagination('consPagination', 'consCountLabel', _consPage, total, _CONS_PAGE_SIZE, '_consGoTo', label);
}

function _consGoTo(p)   { _consPage = p; _renderConsPage(); document.getElementById('consTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
function _consSetSize(v) { _CONS_PAGE_SIZE = Math.max(1, parseInt(v, 10) || 50); _consPage = 0; _renderConsPage(); }

function _consRow(i) {
  const tipoColor = i.type === 'AFK' ? 'var(--warn)' : (i.type === 'Internet' ? '#6c8cff' : 'var(--text)');
  const catCell   = i.category === 'afk'
    ? '<span style="color:var(--text-dim);font-size:11px">—</span>'
    : (CAT_PILLS[i.category] || CAT_PILLS.uncategorized);
  const app = escHtml(i.app || '—');

  let when, title;
  if (_consTimeline) {
    const ini = _consTime(i.start_ts), fim = _consTime(i.end_ts);
    when  = (ini === fim) ? ini : `${ini}–${fim}`;
    title = escHtml(i.title || '');
  } else {
    when  = i.last_access ? ptDateTime(i.last_access) : '—';
    title = (document.getElementById('consGroupMode')?.value === 'byapp') ? '' : escHtml(i.title || '');
  }

  return `<tr>
    <td style="font-size:12px;white-space:nowrap">${when}</td>
    <td style="white-space:nowrap">${fmtSec(i.total_sec || 0)}</td>
    <td><strong style="font-size:12px">${app}</strong>${title ? `<br><span style="color:var(--text-dim);font-size:11px">${title}</span>` : ''}</td>
    <td style="font-size:12px;color:${tipoColor};font-weight:600">${i.type}</td>
    <td>${catCell}</td>
  </tr>`;
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
