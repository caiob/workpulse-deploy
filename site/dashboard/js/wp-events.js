// =====================================================================
// wp-events.js — Aba "Eventos" (coleta completa com produtividade)
// =====================================================================

/* ---------- Estado de paginação ---------- */
let _evPageSize = 100;  // eventos brutos por página (sincronizado com #evPageSize)
let _evPage     = 0;    // página atual (0-indexed)
let _evTotal    = 0;    // total de eventos retornado pelo backend
let _evShowAll  = false; // true = consulta explícita de TODOS (botão "Ver todos")

/** Lê o valor atual do seletor de itens por página. */
function _evGetPageSize() {
  const sel = document.getElementById('evPageSize');
  return sel ? parseInt(sel.value, 10) : _evPageSize;
}

/** Chamado quando o usuário muda o seletor "Por página". */
function _evChangePageSize() {
  _evPageSize = _evGetPageSize();
  _evPage = 0;
  loadEvents(false);
}

// ─────────────────────────────────────────────────────────────────────
// Agrupamento de sessões
// ─────────────────────────────────────────────────────────────────────

/** Retorna o modo de agrupamento selecionado: 'sequential' | 'byapp' | 'none' */
function _evGroupMode() {
  return document.getElementById('evGroupMode')?.value || 'sequential';
}

/**
 * MODO SEQUENTIAL — Agrupa eventos CONSECUTIVOS com mesmo user_id + app.
 * Ignora título. Eventos chegam ordenados por ts DESC.
 */
function collapseEvents(events) {
  if (!events.length) return events;

  const merged = [];
  let cur = {
    ...events[0],
    _count:      1,
    _ts_display: events[0].ts,
    _subEvents:  [events[0]],
  };

  for (let i = 1; i < events.length; i++) {
    const e        = events[i];
    const sameUser = cur.user_id === e.user_id;
    const sameApp  = ((cur.data || {}).app || '').toLowerCase() === ((e.data || {}).app || '').toLowerCase();

    if (sameUser && sameApp) {
      cur.duration_sec = (cur.duration_sec || 0) + (e.duration_sec || 0);
      cur._count++;
      cur._ts_display = e.ts;   // mais antigo = início da sessão
      cur._subEvents.push(e);
    } else {
      merged.push(cur);
      cur = { ...e, _count: 1, _ts_display: e.ts, _subEvents: [e] };
    }
  }
  merged.push(cur);
  return merged;
}

/** Retorna a categoria mais frequente entre um array de eventos. */
function _dominantCategory(events) {
  const cnt = {};
  for (const e of events) {
    const c = e.category || 'uncategorized';
    cnt[c] = (cnt[c] || 0) + 1;
  }
  return Object.entries(cnt).sort((a, b) => b[1] - a[1])[0]?.[0] || 'uncategorized';
}

/**
 * MODO BY-APP — Agrupa TODOS os eventos do período pelo nome do executável,
 * independente de ordem. Ordena por tempo total decrescente.
 * Sub-eventos ficam em _subEvents para expansão.
 */
function groupByApp(events) {
  if (!events.length) return events;

  const appMap = new Map();

  for (const e of events) {
    const app = (e.data || {}).app || '(desconhecido)';
    const key = app.toLowerCase();
    if (!appMap.has(key)) {
      appMap.set(key, {
        ...e,
        data:         { app, title: '' },
        _count:       0,
        duration_sec: 0,
        _subEvents:   [],
        _ts_display:  e.ts,  // mais recente do grupo (1º encontrado em ts DESC)
      });
    }
    const g = appMap.get(key);
    g.duration_sec += (e.duration_sec || 0);
    g._count++;
    g._subEvents.push(e);
  }

  const result = [...appMap.values()].sort((a, b) => b.duration_sec - a.duration_sec);
  // Assign categoria dominante após construir todos os grupos
  for (const g of result) g.category = _dominantCategory(g._subEvents);
  return result;
}

// ─────────────────────────────────────────────────────────────────────
// Toggle de expansão de grupo
// ─────────────────────────────────────────────────────────────────────

function _toggleEvGroup(rowId) {
  const rows = document.querySelectorAll(`.ev-sub-${rowId}`);
  let anyVisible = false;
  rows.forEach(r => { if (!r.classList.contains('hide')) anyVisible = true; });
  rows.forEach(r => r.classList.toggle('hide', anyVisible));

  const btn = document.getElementById(`ev-btn-${rowId}`);
  if (btn) {
    btn.classList.toggle('expanded', !anyVisible);
    btn.textContent = anyVisible ? '▶' : '▼';
  }
}

// ─────────────────────────────────────────────────────────────────────
// Carregamento principal
// ─────────────────────────────────────────────────────────────────────

/**
 * Carrega eventos do servidor para a página atual (_evPage).
 * @param {boolean} resetPage  Se true, volta para a página 0 (usado quando filtros mudam).
 */
async function loadEvents(resetPage = true) {
  if (resetPage) _evPage = 0;

  await _populateUserDropdown('evUser');
  if (typeof _applyDeepLinkUser === 'function') _applyDeepLinkUser('evUser');

  const { since, until } = wpDP.getRange('evDays');
  const user   = document.getElementById('evUser').value.trim();
  // Abre vazio: a consulta aqui é por colaborador. Só busca todos via "Ver todos".
  if (!user && !_evShowAll) { _renderEvPrompt(); return; }
  const bucket = document.getElementById('evBucket').value;
  const sig    = _tabAbort ? _tabAbort.signal : undefined;

  _evPageSize = _evGetPageSize();
  const mode  = _evGroupMode();

  const base      = `tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}&no_browser=true`;
  const userParam = user   ? `&user_id=${encodeURIComponent(user)}` : '';
  const bktParam  = bucket ? `&bucket=${encodeURIComponent(bucket)}` : '';

  // "Agrupar por App" busca tudo de uma vez (sem paginação) — máx 1000 do backend
  const pageParam = mode === 'byapp'
    ? `&limit=1000&offset=0`
    : `&limit=${_evPageSize + 1}&offset=${_evPage * _evPageSize}`;  // N+1 para detectar próxima pág

  try {
    const [summary, evData] = await Promise.all([
      api(`/v1/productivity/events_summary?${base}${userParam}${bktParam}`, sig),
      api(`/v1/events?${base}${userParam}${bktParam}${pageParam}`, sig),
    ]);

    // KPIs
    const t     = summary.totals || {};
    const total = t.total || 0;
    const pctOf = v => total > 0 ? ` <small>${(v / total * 100).toFixed(0)}%</small>` : '';
    document.getElementById('kpiEvTotal').innerHTML  = fmtSec(total);
    document.getElementById('kpiEvProd').innerHTML   = fmtSec(t.productive   || 0) + pctOf(t.productive   || 0);
    document.getElementById('kpiEvUnprod').innerHTML = fmtSec(t.unproductive || 0) + pctOf(t.unproductive || 0);
    document.getElementById('kpiEvUncat').innerHTML  = fmtSec(t.uncategorized || 0) + pctOf(t.uncategorized || 0);

    drawEvDailyChart(summary.daily || []);

    // ── Paginação + agrupamento dependem do modo ──────────────────────
    let rawEvents, hasNext, hasPrev, totalPages;

    if (mode === 'byapp') {
      // Sem paginação — todos os eventos já vieram
      rawEvents  = evData.events || [];
      hasNext    = false;
      hasPrev    = false;
      totalPages = 1;
      _evTotal   = evData.total || rawEvents.length;
    } else {
      // N+1: se vieram mais que _evPageSize há próxima página
      const rawAll = evData.events || [];
      hasNext      = rawAll.length > _evPageSize;
      rawEvents    = hasNext ? rawAll.slice(0, _evPageSize) : rawAll;
      hasPrev      = _evPage > 0;
      _evTotal     = evData.total || 0;
      totalPages   = _evTotal > 0
        ? Math.max(1, Math.ceil(_evTotal / _evPageSize))
        : (hasNext ? _evPage + 2 : _evPage + 1);
    }

    // Filtro de categoria (aplicado antes do agrupamento)
    const catFilter  = document.getElementById('evCategory')?.value || '';
    const filtered   = catFilter ? rawEvents.filter(e => e.category === catFilter) : rawEvents;

    // Aplica o modo de agrupamento sobre os eventos já filtrados
    const events = mode === 'sequential' ? collapseEvents(filtered)
                 : mode === 'byapp'      ? groupByApp(filtered)
                 : filtered;  // 'none'

    // Label de contagem
    const label = document.getElementById('evCountLabel');
    if (label) {
      const catEl     = document.getElementById('evCategory');
      const catLabel  = catEl?.value ? ` · ${catEl.options[catEl.selectedIndex].text}` : '';
      if (!rawEvents.length && !_evPage) {
        label.textContent = '';
      } else if (mode === 'byapp') {
        label.textContent = `— ${events.length} apps (${filtered.length} aplicativos${catLabel})`;
      } else if (mode === 'sequential' && events.length < filtered.length) {
        const saved = filtered.length - events.length;
        label.textContent = `— ${events.length} grupos (${filtered.length} aplicativos, ${saved} agrupados${catLabel})`;
      } else if (_evTotal > 0) {
        label.textContent = `— ${filtered.length} de ${_evTotal.toLocaleString()} eventos${catLabel}`;
      } else {
        label.textContent = `— ${filtered.length} evento${filtered.length !== 1 ? 's' : ''}${catLabel}`;
      }
    }

    renderEventRows(events);
    _renderEvPagination();
  } catch (e) {
    _setConnectionStatus('erro', true);
    toast('Falha ao carregar eventos: ' + e.message);
  }
}

// Loader da aba: abre vazio (reseta o modo "ver todos") — a consulta é por pessoa.
function openEvents() { _evShowAll = false; loadEvents(); }

// Trocou o usuário/filtro: sai do modo "ver todos" e reflete a seleção atual.
function applyEvents() { _evShowAll = false; loadEvents(); }

// Visão completa (todos os colaboradores) — ação explícita, pode demorar.
function loadEventsAll() {
  _evShowAll = true;
  const s = document.getElementById('evUser'); if (s) s.value = '';
  loadEvents();
}

// Estado inicial / vazio: limpa KPIs, gráfico, tabela e mostra a instrução.
function _renderEvPrompt() {
  ['kpiEvTotal', 'kpiEvProd', 'kpiEvUnprod', 'kpiEvUncat'].forEach(id => {
    const e = document.getElementById(id); if (e) e.innerHTML = '—';
  });
  if (state.charts.evDaily) { state.charts.evDaily.destroy(); state.charts.evDaily = null; }
  _evPage = 0; _evTotal = 0;
  const lbl = document.getElementById('evCountLabel'); if (lbl) lbl.textContent = '';
  const pag = document.getElementById('evPagination'); if (pag) pag.innerHTML = '';
  const tbody = document.querySelector('#eventsTable tbody');
  if (tbody) tbody.innerHTML = `<tr><td colspan="6" class="empty-state">
      <h4>Selecione um colaborador</h4>
      Escolha alguém no filtro <strong>Usuário</strong> acima para ver os aplicativos dele no período.
      <div style="margin-top:10px"><button class="ghost" onclick="loadEventsAll()">Ver todos (pode demorar)</button></div>
    </td></tr>`;
}

// ─────────────────────────────────────────────────────────────────────
// Chart diário de eventos
// ─────────────────────────────────────────────────────────────────────

function drawEvDailyChart(daily) {
  if (state.charts.evDaily) state.charts.evDaily.destroy();
  if (!daily.length) return;
  const ct  = chartTheme();
  const ctx = document.getElementById('chartEvDaily').getContext('2d');
  state.charts.evDaily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => ptDate(d.day + 'T12:00:00')),
      datasets: [
        { label: 'Produtivo',        data: daily.map(d => +(d.productive    / 3600).toFixed(2)), backgroundColor: CAT_COLORS.productive,    borderRadius: 4, stack: 'a' },
        { label: 'Não-produtivo',    data: daily.map(d => +(d.unproductive  / 3600).toFixed(2)), backgroundColor: CAT_COLORS.unproductive,  borderRadius: 4, stack: 'a' },
        { label: 'Neutro',           data: daily.map(d => +(d.neutral       / 3600).toFixed(2)), backgroundColor: CAT_COLORS.neutral,        borderRadius: 4, stack: 'a' },
        { label: 'Não categorizado', data: daily.map(d => +(d.uncategorized / 3600).toFixed(2)), backgroundColor: CAT_COLORS.uncategorized,  borderRadius: 4, stack: 'a' },
      ],
    },
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

// ─────────────────────────────────────────────────────────────────────
// Renderização da tabela de eventos
// ─────────────────────────────────────────────────────────────────────

function renderEventRows(events) {
  const tbody = document.querySelector('#eventsTable tbody');
  if (!tbody) return;

  if (!events.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">Nenhum evento encontrado para este período e filtros.</td></tr>';
    return;
  }

  tbody.innerHTML = events.map((e, idx) => {
    const rowId    = 'ev-' + idx;
    const when     = e._ts_display || e.ts;
    const whenFmt  = when ? ptDateTime(when) : '—';
    const uid      = cleanUserId(e.user_id || '');
    const uObj     = state.people.find(p => cleanUserId(p.user_id) === uid);
    const uName    = uObj ? displayName(uObj) : uid;
    const dur      = fmtSec(e.duration_sec || 0);
    const app      = escHtml((e.data || {}).app || '—');
    const title    = escHtml((e.data || {}).title || '');
    const catBadge = CAT_PILLS[e.category] || CAT_PILLS.uncategorized;
    const isGroup  = (e._count || 1) > 1;

    const mainRow = `<tr>
      <td style="font-size:12px;white-space:nowrap">${whenFmt}</td>
      <td style="font-size:12px">${escHtml(uName)}</td>
      <td style="white-space:nowrap">${dur}${isGroup ? ` <small>(${e._count})</small>` : ''}</td>
      <td><strong style="font-size:12px">${app}</strong>${title ? `<br><span style="color:var(--text-dim);font-size:11px">${title}</span>` : ''}</td>
      <td>${catBadge}</td>
      <td style="text-align:center">
        ${isGroup ? `<button id="ev-btn-${rowId}" class="ghost ev-page-btn" style="font-size:10px;padding:2px 6px" onclick="_toggleEvGroup('${rowId}')">▶</button>` : ''}
      </td>
    </tr>`;

    let subRows = '';
    if (isGroup && e._subEvents) {
      subRows = e._subEvents.map(se => {
        const sWhen  = se.ts ? ptDateTime(se.ts) : '—';
        const sDur   = fmtSec(se.duration_sec || 0);
        const sApp   = escHtml((se.data || {}).app || '—');
        const sTitle = escHtml((se.data || {}).title || '');
        const sCat   = CAT_PILLS[se.category] || CAT_PILLS.uncategorized;
        return `<tr class="ev-sub-${rowId} hide" style="background:rgba(0,0,0,.08);font-size:11px">
          <td style="padding-left:20px;font-size:11px">${sWhen}</td>
          <td></td>
          <td>${sDur}</td>
          <td>${sApp}${sTitle ? `<br><span style="color:var(--text-dim)">${sTitle}</span>` : ''}</td>
          <td>${sCat}</td>
          <td></td>
        </tr>`;
      }).join('');
    }

    return mainRow + subRows;
  }).join('');
}

// ─────────────────────────────────────────────────────────────────────
// Paginação de eventos (server-side)
// ─────────────────────────────────────────────────────────────────────

function _renderEvPagination() {
  const container = document.getElementById('evPagination');
  if (!container) return;

  const totalPages = _evTotal > 0
    ? Math.max(1, Math.ceil(_evTotal / _evPageSize))
    : _evPage + 1;
  const hasPrev = _evPage > 0;
  const hasNext = _evTotal > 0
    ? (_evPage + 1) * _evPageSize < _evTotal
    : false;

  if (totalPages <= 1 && !hasPrev && !hasNext) {
    container.innerHTML = '';
    return;
  }

  const showMax = 5;
  let start = Math.max(0, _evPage - 2);
  let end   = Math.min(totalPages - 1, start + showMax - 1);
  if (end - start < showMax - 1) start = Math.max(0, end - showMax + 1);

  let numbersHtml = '';
  if (start > 0) numbersHtml += '<span class="ev-page-ellipsis">…</span>';
  for (let i = start; i <= end; i++) {
    numbersHtml += `<button class="ghost ev-page-btn${i === _evPage ? ' ev-page-active' : ''}"
      onclick="_evPage=${i};loadEvents(false)">${i + 1}</button>`;
  }
  if (end < totalPages - 1) numbersHtml += '<span class="ev-page-ellipsis">…</span>';

  container.innerHTML = `
    <div class="ev-pagination">
      <button class="ghost ev-page-nav" ${hasPrev ? '' : 'disabled'}
        onclick="_evPage=${_evPage - 1};loadEvents(false)">← Anterior</button>
      <div class="ev-page-numbers">${numbersHtml}</div>
      <button class="ghost ev-page-nav" ${hasNext ? '' : 'disabled'}
        onclick="_evPage=${_evPage + 1};loadEvents(false)">Próxima →</button>
    </div>`;
}
