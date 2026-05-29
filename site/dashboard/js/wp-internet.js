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
  const days   = document.getElementById('intDays').value;
  const user   = document.getElementById('intUser').value.trim();
  const params = new URLSearchParams({ tenant: state.tenant, days });
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
  const totalPages = Math.max(1, Math.ceil(totalSites / _intPageSize));

  // Garante que a página atual não ultrapasse o total
  if (_intPage >= totalPages) _intPage = totalPages - 1;

  const hasPrev = _intPage > 0;
  const hasNext = _intPage < totalPages - 1;

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

  const pageSlice = sites.slice(_intPage * _intPageSize, (_intPage + 1) * _intPageSize);

  tbody.innerHTML = pageSlice.map(s => {
    const pct     = totalSec > 0 ? (s.total_sec / totalSec * 100).toFixed(1) : '0';
    const barFill = CAT_COLORS[s.category] || CAT_COLORS.uncategorized;
    const bar     = `<div class="bar"><span style="width:${Math.min(+pct, 100)}%;background:${barFill}"></span></div>`;
    const pill    = CAT_PILLS[s.category] || CAT_PILLS.uncategorized;
    const users   = s.user_count === 1 ? '1 usuário' : s.user_count + ' usuários';
    const safeName = s.site_name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    const when    = s.last_access
      ? new Date(s.last_access).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '—';
    return `<tr>
      <td style="white-space:nowrap;color:var(--text-dim);font-size:12px">${when}</td>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${s.site_name}">${s.site_name}</td>
      <td>${pill}</td>
      <td>${fmtSec(s.total_sec)}</td>
      <td>${bar} <span style="color:var(--text-dim);font-size:11px">${pct}%</span></td>
      <td style="color:var(--text-dim)">${users}</td>
      <td><button class="cat-btn" onclick="quickRule('${safeName}')">Categorizar</button></td>
    </tr>`;
  }).join('');

  _renderIntPagination(hasPrev, hasNext, totalPages, totalSites);
}

// ─────────────────────────────────────────────────────────────────────
// Paginação
// ─────────────────────────────────────────────────────────────────────

function _renderIntPagination(hasPrev, hasNext, totalPages, totalSites) {
  const container = document.getElementById('intPagination');
  if (!container) return;

  // Label de contagem no cabeçalho
  const label = document.getElementById('intCountLabel');
  if (label) {
    const catEl    = document.getElementById('intCategory');
    const catLabel = catEl?.value ? ` · ${catEl.options[catEl.selectedIndex].text}` : '';
    label.textContent = totalSites > 0
      ? `— ${totalSites.toLocaleString('pt-BR')} sites${catLabel}`
      : '';
  }

  if (!hasPrev && !hasNext) {
    container.innerHTML = '';
    return;
  }

  const currentPage = _intPage + 1;
  const windowStart = Math.max(1, currentPage - 2);
  const windowEnd   = Math.min(totalPages, currentPage + 2);

  const pageButtons = [];

  if (windowStart > 1) {
    pageButtons.push(`<button class="ev-page-btn ghost" onclick="_intGoTo(0)">1</button>`);
    if (windowStart > 2) pageButtons.push(`<span class="ev-page-ellipsis">…</span>`);
  }

  for (let p = windowStart; p <= windowEnd; p++) {
    const active = p === currentPage ? ' ev-page-active' : '';
    pageButtons.push(
      `<button class="ev-page-btn ghost${active}" onclick="_intGoTo(${p - 1})">${p}</button>`
    );
  }

  if (windowEnd < totalPages) {
    if (windowEnd < totalPages - 1) pageButtons.push(`<span class="ev-page-ellipsis">…</span>`);
    pageButtons.push(`<button class="ev-page-btn ghost" onclick="_intGoTo(${totalPages - 1})">${totalPages}</button>`);
  }

  container.innerHTML = `
    <div class="ev-pagination">
      <button class="ghost ev-page-nav" ${hasPrev ? '' : 'disabled'} onclick="_intGoTo(${_intPage - 1})">← Anterior</button>
      <div class="ev-page-numbers">${pageButtons.join('')}</div>
      <button class="ghost ev-page-nav" ${hasNext ? '' : 'disabled'} onclick="_intGoTo(${_intPage + 1})">Próxima →</button>
    </div>`;
}

/** Navega para uma página específica e rerenderiza. */
function _intGoTo(page) {
  _intPage = page;
  renderSitesTable(_intFiltered, _intTotalCache);
  document.getElementById('intSitesTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
