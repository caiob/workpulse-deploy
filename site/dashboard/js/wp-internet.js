// =====================================================================
// wp-internet.js — Aba "Internet" (análise de navegação web)
// =====================================================================

// Cache da última busca — permite filtrar por categoria sem nova chamada
let _intSitesCache  = [];
let _intTotalCache  = 0;
let _intFiltered    = [];   // cache pós-filtro de categoria
let _intPage        = 0;    // página atual (0-indexed)
let _intPageSize    = 100;  // itens por página

/** Lê o valor atual do seletor de itens por página. */
function _intGetPageSize() {
  const sel = document.getElementById('intPageSize');
  return sel ? parseInt(sel.value, 10) : _intPageSize;
}

/** Chamado quando o usuário muda o seletor "Por página". */
function _intChangePageSize() {
  _intPageSize = _intGetPageSize();
  _intPage = 0;
  renderSitesTable(_intFiltered, _intTotalCache);
}

async function loadInternet() {
  await _populateUserDropdown('intUser');
  const { since, until } = wpDP.getRange('intDays');
  const user   = document.getElementById('intUser').value.trim();
  const params = new URLSearchParams({ tenant: state.tenant, since, until });
  if (user) params.set('user_id', user);

  // Reseta o filtro de categoria e a página ao recarregar
  const catSel = document.getElementById('intCategory');
  if (catSel) catSel.value = '';
  _intPage = 0;

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

    // Armazena no cache e renderiza
    _intSitesCache = data.sites || [];
    _intTotalCache = total;
    _intFiltered   = _intSitesCache;
    renderSitesTable(_intFiltered, _intTotalCache);
  } catch (e) { toast('Erro ao carregar dados de internet: ' + e.message); }
}

/**
 * Reaplica o filtro de categoria sobre o cache sem nova chamada à API.
 * Chamada pelo onchange do select #intCategory.
 */
function filterInternet() {
  const cat = document.getElementById('intCategory')?.value || '';
  _intFiltered = cat ? _intSitesCache.filter(s => s.category === cat) : _intSitesCache;
  _intPage = 0;
  renderSitesTable(_intFiltered, _intTotalCache, cat);
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

/**
 * @param {Array}  sites      Lista de sites a renderizar (já filtrada por categoria)
 * @param {number} totalSec   Total geral do período (para % — nunca filtrado)
 * @param {string} [catFilter] Categoria ativa (para feedback no empty-state)
 */
function renderSitesTable(sites, totalSec, catFilter = '') {
  _intPageSize = _intGetPageSize();

  const tbody     = document.querySelector('#intSitesTable tbody');
  const totalSites = sites.length;
  // Garante que a página atual não ultrapasse o total
  const _totalPagesCheck = Math.max(1, Math.ceil(totalSites / _intPageSize));
  if (_intPage >= _totalPagesCheck) _intPage = _totalPagesCheck - 1;

  if (!_intSitesCache.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">
      <h4>Nenhum dado de browser encontrado.</h4>
      Verifique se há eventos do tipo <code>window</code> com app de browser (msedge.exe, chrome.exe, firefox.exe...).
    </td></tr>`;
    _renderIntPagination(false, false, 1, 0);
    return;
  }

  if (!sites.length) {
    const catEl  = document.getElementById('intCategory');
    const catTxt = catEl ? catEl.options[catEl.selectedIndex].text : catFilter;
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Nenhum site encontrado na categoria <strong>${catTxt}</strong> para este período.</td></tr>`;
    _renderIntPagination(false, false, 1, 0);
    return;
  }

  // Slice for current page
  const start   = _intPage * _intPageSize;
  const pageRows = sites.slice(start, start + _intPageSize);
  const totalPages = Math.ceil(sites.length / _intPageSize);
  const hasPrev = _intPage > 0;
  const hasNext = (_intPage + 1) < totalPages;

  tbody.innerHTML = pageRows.map(s => {
    const pct   = totalSec > 0 ? Math.round(s.total_sec / totalSec * 100) : 0;
    const users = (s.users || []).map(u => {
      const found = (state.lastTeam?.users || []).find(x => x.user_id === u);
      return found ? displayName(found) : u.slice(0, 8);
    }).join(', ');
    const catBadge = _intCatBadge(s.category);
    const when = s.last_access ? ptDateTime(s.last_access) : '—';
    return `<tr>
      <td>${when}</td>
      <td class="site-name">${escHtml(s.site_name)}</td>
      <td>${fmtH(s.total_sec)}h <small>(${pct}%)</small></td>
      <td>${catBadge} <button class="ghost" style="font-size:11px;padding:2px 6px"
            onclick="prefillRule(${JSON.stringify(s.site_name)})">Categorizar</button></td>
      <td>${s.user_count}</td>
      <td style="font-size:11px;color:var(--text-dim)">${users}</td>
    </tr>`;
  }).join('');

  _renderIntPagination(hasPrev, hasNext, totalPages, sites.length);
}

// ─────────────────────────────────────────────────────────────────────
// Badge de categoria
// ─────────────────────────────────────────────────────────────────────

function _intCatBadge(cat) {
  return CAT_PILLS[cat] || CAT_PILLS.uncategorized;
}

// ─────────────────────────────────────────────────────────────────────
// Paginação client-side para Internet
// ─────────────────────────────────────────────────────────────────────

function _renderIntPagination(hasPrev, hasNext, totalPages, totalItems) {
  const container = document.getElementById('intPagination');
  if (!container) return;

  if (totalPages <= 1 && !hasPrev && !hasNext) {
    container.innerHTML = '';
    return;
  }

  const showMax = 5;
  let start = Math.max(0, _intPage - 2);
  let end   = Math.min(totalPages - 1, start + showMax - 1);
  if (end - start < showMax - 1) start = Math.max(0, end - showMax + 1);

  let numbersHtml = '';
  if (start > 0) numbersHtml += '<span class="ev-page-ellipsis">…</span>';
  for (let i = start; i <= end; i++) {
    numbersHtml += `<button class="ghost ev-page-btn${i === _intPage ? ' ev-page-active' : ''}"
      onclick="_intPage=${i};renderSitesTable(_intFiltered,_intTotalCache)">${i + 1}</button>`;
  }
  if (end < totalPages - 1) numbersHtml += '<span class="ev-page-ellipsis">…</span>';

  container.innerHTML = `
    <div class="ev-pagination">
      <button class="ghost ev-page-nav" ${hasPrev ? '' : 'disabled'}
        onclick="_intPage=${_intPage - 1};renderSitesTable(_intFiltered,_intTotalCache)">← Anterior</button>
      <div class="ev-page-numbers">${numbersHtml}</div>
      <button class="ghost ev-page-nav" ${hasNext ? '' : 'disabled'}
        onclick="_intPage=${_intPage + 1};renderSitesTable(_intFiltered,_intTotalCache)">Próxima →</button>
    </div>`;
}
