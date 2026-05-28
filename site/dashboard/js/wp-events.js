// =====================================================================
// wp-events.js — Aba "Eventos" (coleta completa com produtividade)
// =====================================================================

/* ---------- Estado de paginação ---------- */
let _evPageSize = 100;  // eventos brutos por página (sincronizado com #evPageSize)
let _evPage     = 0;    // página atual (0-indexed)
let _evTotal    = 0;    // total de eventos retornado pelo backend

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

/**
 * Agrupa eventos consecutivos com mesmo user_id + app (ignora título).
 * Eventos chegam ordenados por ts DESC (mais recente primeiro).
 *
 * Cada grupo resultante contém:
 *   _count      — quantos eventos foram mesclados
 *   _ts_display — timestamp do evento mais antigo (início da sessão)
 *   _subEvents  — array com todos os eventos individuais (para expansão)
 *   data.title  — título do evento mais recente do grupo
 */
function collapseEvents(events) {
  if (!events.length) return events;

  const merged = [];
  let cur = {
    ...events[0],
    _count:     1,
    _ts_display: events[0].ts,
    _subEvents: [events[0]],
  };

  for (let i = 1; i < events.length; i++) {
    const e       = events[i];
    const sameUser = cur.user_id === e.user_id;
    const sameApp  = ((cur.data || {}).app || '') === ((e.data || {}).app || '');

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

  const days   = document.getElementById('evDays').value;
  const user   = document.getElementById('evUser').value.trim();
  const bucket = document.getElementById('evBucket').value;
  const sig    = _tabAbort ? _tabAbort.signal : undefined;

  _evPageSize = _evGetPageSize();

  const base      = `tenant=${encodeURIComponent(state.tenant)}&days=${days}`;
  const userParam = user   ? `&user_id=${encodeURIComponent(user)}` : '';
  const bktParam  = bucket ? `&bucket=${encodeURIComponent(bucket)}` : '';
  // N+1: pede um evento a mais para detectar se existe próxima página
  const pageParam = `&limit=${_evPageSize + 1}&offset=${_evPage * _evPageSize}`;

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

    // N+1: determina hasNext e descarta o evento extra
    const rawAll    = evData.events || [];
    const hasNext   = rawAll.length > _evPageSize;
    const rawEvents = hasNext ? rawAll.slice(0, _evPageSize) : rawAll;

    // Total do backend (quando disponível) para exibir "Página X de Y"
    _evTotal = evData.total || 0;
    const totalPages = _evTotal > 0
      ? Math.max(1, Math.ceil(_evTotal / _evPageSize))
      : (hasNext ? _evPage + 2 : _evPage + 1);

    const hasPrev    = _evPage > 0;
    const doCollapse = document.getElementById('evCollapse')?.checked !== false;
    const events     = doCollapse ? collapseEvents(rawEvents) : rawEvents;

    // Label de contagem
    const label = document.getElementById('evCountLabel');
    if (label) {
      if (!rawEvents.length && !_evPage) {
        label.textContent = '';
      } else if (doCollapse && events.length < rawEvents.length) {
        const saved = rawEvents.length - events.length;
        label.textContent = `— ${events.length} grupos (${rawEvents.length} eventos, ${saved} agrupados)`;
      } else if (_evTotal > 0) {
        label.textContent = `— ${rawEvents.length} de ${_evTotal.toLocaleString('pt-BR')} eventos`;
      } else {
        label.textContent = `— ${rawEvents.length} eventos`;
      }
    }

    const tb = document.querySelector('#eventsTable tbody');
    if (!events.length) {
      tb.innerHTML = '<tr><td colspan="6" class="empty-state">Nenhum evento encontrado com estes filtros.</td></tr>';
      _renderEvPagination(hasPrev, hasNext, totalPages);
      return;
    }

    tb.innerHTML = events.map((e, idx) => {
      const rowId    = `p${_evPage}_${idx}`;
      const d        = e.data || {};
      const appLabel = d.app ? `<strong>${d.app}</strong>` : '<span style="color:var(--text-muted)">—</span>';
      const title    = d.title || '';
      const cat      = CAT_PILLS[e.category] || CAT_PILLS.uncategorized;
      const when     = e._ts_display || e.ts;
      const ts       = when
        ? new Date(when).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '—';
      const userName  = displayName(state.people.find(p => p.user_id === e.user_id) || { user_id: e.user_id });
      const dur       = fmtSec(e.duration_sec || 0);
      const safePat   = (d.title || d.app || '').replace(/'/g, "\\'");
      const safeField = d.title ? 'title' : 'app';
      const isGroup   = (e._count || 1) > 1;

      // Badge de contagem e botão de expansão
      const countBadge = isGroup
        ? `<span class="ev-count-badge">${e._count}×</span>`
        : '';
      const expandBtn = isGroup
        ? `<button id="ev-btn-${rowId}" class="ev-expand-btn ghost" onclick="_toggleEvGroup('${rowId}')" title="Expandir eventos individuais">▶</button>`
        : '';

      // Linha pai
      const parentRow = `<tr class="${isGroup ? 'ev-group-row' : ''}">
        <td style="white-space:nowrap;color:var(--text-dim);font-size:12px">${ts}</td>
        <td>${userName}</td>
        <td>${dur}${countBadge}</td>
        <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${appLabel}${title ? ` <span style="color:var(--text-dim);font-size:11px">— ${title}</span>` : ''}
        </td>
        <td>${cat}</td>
        <td style="display:flex;gap:6px;align-items:center">
          ${expandBtn}
          <button class="cat-btn" onclick="quickRule('${safePat}','${safeField}')">Categorizar</button>
        </td>
      </tr>`;

      // Sub-linhas (ocultas por padrão)
      const subRows = isGroup
        ? e._subEvents.map((sub, si) => {
            const sd    = sub.data || {};
            const sTs   = sub.ts
              ? new Date(sub.ts).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
              : '—';
            const sDur  = fmtSec(sub.duration_sec || 0);
            const sTitle = sd.title || '';
            const sPat  = (sd.title || sd.app || '').replace(/'/g, "\\'");
            const sField = sd.title ? 'title' : 'app';
            return `<tr class="ev-sub-row ev-sub-${rowId} hide">
              <td style="white-space:nowrap;color:var(--text-dim);font-size:11px;padding-left:28px">↳ ${sTs}</td>
              <td></td>
              <td style="color:var(--text-dim);font-size:12px">${sDur}</td>
              <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:var(--text-dim)">
                ${sTitle || '<em style="opacity:.5">sem título</em>'}
              </td>
              <td></td>
              <td><button class="cat-btn" style="font-size:11px" onclick="quickRule('${sPat}','${sField}')">Categorizar</button></td>
            </tr>`;
          }).join('')
        : '';

      return parentRow + subRows;
    }).join('');

    _renderEvPagination(hasPrev, hasNext, totalPages);

  } catch (e) { toast('Erro ao carregar eventos: ' + e.message); }
}

// ─────────────────────────────────────────────────────────────────────
// Paginação
// ─────────────────────────────────────────────────────────────────────

/**
 * Renderiza os controles de paginação no cabeçalho do card.
 */
function _renderEvPagination(hasPrev, hasNext, totalPages) {
  const container = document.getElementById('evPagination');
  if (!container) return;

  if (!hasPrev && !hasNext) {
    container.innerHTML = '';
    return;
  }

  const currentPage = _evPage + 1;
  const windowStart = Math.max(1, currentPage - 2);
  const windowEnd   = Math.min(totalPages, currentPage + 2);

  const pageButtons = [];

  if (windowStart > 1) {
    pageButtons.push(`<button class="ev-page-btn ghost" onclick="_evGoTo(0)">1</button>`);
    if (windowStart > 2) pageButtons.push(`<span class="ev-page-ellipsis">…</span>`);
  }

  for (let p = windowStart; p <= windowEnd; p++) {
    const active = p === currentPage ? ' ev-page-active' : '';
    pageButtons.push(
      `<button class="ev-page-btn ghost${active}" onclick="_evGoTo(${p - 1})">${p}</button>`
    );
  }

  if (windowEnd < totalPages) {
    if (windowEnd < totalPages - 1) pageButtons.push(`<span class="ev-page-ellipsis">…</span>`);
    pageButtons.push(`<button class="ev-page-btn ghost" onclick="_evGoTo(${totalPages - 1})">${totalPages}</button>`);
  }

  container.innerHTML = `
    <div class="ev-pagination">
      <button class="ghost ev-page-nav" ${hasPrev ? '' : 'disabled'} onclick="_evGoTo(${_evPage - 1})">← Anterior</button>
      <div class="ev-page-numbers">${pageButtons.join('')}</div>
      <button class="ghost ev-page-nav" ${hasNext ? '' : 'disabled'} onclick="_evGoTo(${_evPage + 1})">Próxima →</button>
    </div>`;
}

/** Navega para uma página específica e recarrega. */
function _evGoTo(page) {
  _evPage = page;
  loadEvents(false);
  document.getElementById('eventsTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ─────────────────────────────────────────────────────────────────────
// Gráfico diário
// ─────────────────────────────────────────────────────────────────────

function drawEvDailyChart(daily) {
  if (state.charts.evDaily) state.charts.evDaily.destroy();
  if (!daily.length) return;
  const ct  = chartTheme();
  const ctx = document.getElementById('chartEvDaily').getContext('2d');
  state.charts.evDaily = new Chart(ctx, {
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
