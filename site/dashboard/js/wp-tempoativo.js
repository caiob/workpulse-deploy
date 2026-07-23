// =====================================================================
// wp-tempoativo.js — Aba "Tempo Ativo" (Análises)
// Todos os colaboradores no período, com busca, ordenação (maior/menor
// tempo ativo e AFK), filtro por departamento (múltipla seleção), KPIs
// e gráfico horizontal. Fonte: /v1/team/summary (mesmo da Visão Geral).
// =====================================================================

// AFK considerado = AFK na jornada (afk_in), com fallback para afk total.
// Mantém consistência com o card "Tempo ativo por colaborador" da Visão Geral.
const _taAfkOf = u => (u.afk_in != null ? u.afk_in : u.afk) || 0;

let _taUsers = [];                   // dataset bruto do período
let _taDeptSel = new Set();          // departamentos selecionados (vazio = todos)

async function loadTempoAtivo() {
  try {
    const { since, until } = wpDP.getRange('taDays');
    const qs = `tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`;
    const data = await api(`/v1/team/summary?${qs}`);
    _taUsers = data.users || [];
    markVacation(_taUsers);          // atualiza state.vacation p/ o rótulo 🏖 Férias
    _taBuildDeptMenu();
    taApply();
  } catch (e) {
    toast('Erro ao carregar tempo ativo: ' + e.message);
  }
}

// ---- Multi-select de departamentos ----
function _taDepts() {
  return [...new Set(_taUsers
    .map(u => (u.department || '').trim())
    .filter(Boolean))].sort((a, b) => a.localeCompare(b, 'pt-BR'));
}

function _taBuildDeptMenu() {
  const menu = document.getElementById('taDeptMenu');
  if (!menu) return;
  const depts = _taDepts();
  // Descarta seleções que não existem mais no período atual.
  _taDeptSel.forEach(d => { if (!depts.includes(d)) _taDeptSel.delete(d); });

  const rows = depts.map(d => {
    const esc = d.replace(/"/g, '&quot;');
    const chk = _taDeptSel.has(d) ? 'checked' : '';
    return `<label><input type="checkbox" value="${esc}" ${chk} onchange="taDeptCheck(this)"> ${d}</label>`;
  }).join('') || '<div style="padding:8px;color:var(--text-muted,#8c97c4);font-size:12px">Sem departamentos configurados.</div>';

  menu.innerHTML = `<div class="ta-dept-actions">
      <button type="button" onclick="taDeptAll()">Todos</button>
      <button type="button" onclick="taDeptClear()">Limpar</button>
    </div>${rows}`;
  _taUpdateDeptBtn();
}

function _taUpdateDeptBtn() {
  const btn = document.getElementById('taDeptBtn');
  if (!btn) return;
  let label;
  if (_taDeptSel.size === 0)      label = 'Todos';
  else if (_taDeptSel.size === 1) label = [..._taDeptSel][0];
  else                            label = `${_taDeptSel.size} selecionados`;
  btn.innerHTML = `${label} <span style="opacity:.6">▾</span>`;
}

function taDeptToggle(e) {
  if (e) e.stopPropagation();
  document.getElementById('taDeptMenu').classList.toggle('hide');
}
function taDeptCheck(cb) {
  if (cb.checked) _taDeptSel.add(cb.value); else _taDeptSel.delete(cb.value);
  _taUpdateDeptBtn();
  taApply();
}
function taDeptAll()   { _taDeptSel.clear(); _taBuildDeptMenu(); taApply(); }
function taDeptClear() { _taDeptSel.clear(); _taBuildDeptMenu(); taApply(); }

// Fecha o menu ao clicar fora.
document.addEventListener('click', e => {
  const wrap = document.getElementById('taDeptWrap');
  const menu = document.getElementById('taDeptMenu');
  if (wrap && menu && !wrap.contains(e.target)) menu.classList.add('hide');
});

// ---- Filtro (busca + departamentos) ----
function _taFiltered() {
  const q = (document.getElementById('taSearch').value || '').toLowerCase().trim();
  return _taUsers.filter(u => {
    if (_taDeptSel.size && !_taDeptSel.has((u.department || '').trim())) return false;
    if (!q) return true;
    return displayName(u).toLowerCase().includes(q)
      || (u.email      || '').toLowerCase().includes(q)
      || (u.user_id    || '').toLowerCase().includes(q)
      || (u.department || '').toLowerCase().includes(q);
  });
}

function _taSorted(list) {
  const mode = (document.getElementById('taSort') || {}).value || 'active_desc';
  const arr = list.slice();
  switch (mode) {
    case 'active_asc': arr.sort((a, b) => (a.active || 0) - (b.active || 0)); break;
    case 'afk_desc':   arr.sort((a, b) => _taAfkOf(b) - _taAfkOf(a)); break;
    case 'afk_asc':    arr.sort((a, b) => _taAfkOf(a) - _taAfkOf(b)); break;
    default:           arr.sort((a, b) => (b.active || 0) - (a.active || 0)); // active_desc
  }
  return arr;
}

function taApply() {
  const filtered = _taFiltered();
  _taRenderKpis(filtered);
  _taRenderChart(_taSorted(filtered));
}

function _taRenderKpis(list) {
  const n = list.length;
  const totalActive = list.reduce((s, u) => s + (u.active || 0), 0);
  const totalAfk    = list.reduce((s, u) => s + _taAfkOf(u), 0);
  const avgActive   = n ? totalActive / n : 0;
  const avgAfk      = n ? totalAfk / n : 0;
  const denom       = totalActive + totalAfk;
  const adh         = denom > 0 ? (totalActive / denom * 100) : 0;

  document.getElementById('taKpiActive').innerHTML    = `${fmtH(totalActive)}<small>h</small>`;
  document.getElementById('taKpiActiveSub').textContent = `${n} colaborador${n === 1 ? '' : 'es'} no filtro`;
  document.getElementById('taKpiActiveAvg').innerHTML = `${fmtH(avgActive)}<small>h</small>`;
  document.getElementById('taKpiAfk').innerHTML       = `${fmtH(avgAfk)}<small>h</small>`;
  document.getElementById('taKpiAdh').innerHTML       = `${adh.toFixed(0)}<small>%</small>`;
}

function _taRenderChart(sorted) {
  const sub = document.getElementById('taChartSub');
  if (sub) sub.textContent = sorted.length
    ? `${sorted.length} colaborador${sorted.length === 1 ? '' : 'es'} · barras somam tempo ativo + AFK na jornada. Passe o mouse para ver os valores.`
    : 'Nenhum colaborador no filtro atual.';

  // Altura dinâmica: ~26px por colaborador (barras horizontais roláveis).
  const box = document.getElementById('taChartBox');
  if (box) box.style.height = Math.max(220, sorted.length * 26 + 60) + 'px';

  const ct = chartTheme();
  if (state.charts.tempoAtivo) state.charts.tempoAtivo.destroy();
  if (!sorted.length) return;

  state.charts.tempoAtivo = new Chart(
    document.getElementById('chartTempoAtivo').getContext('2d'), {
    type: 'bar',
    data: {
      labels: sorted.map(u => displayName(u) + (isVacation(u.user_id) ? '  🏖 Férias' : '')),
      datasets: [
        { label: 'Ativo (h)', data: sorted.map(u => +((u.active || 0) / 3600).toFixed(2)),
          backgroundColor: 'rgba(108,140,255,.85)', borderRadius: 4, stack: 'a' },
        { label: 'AFK na jornada (h)', data: sorted.map(u => +(_taAfkOf(u) / 3600).toFixed(2)),
          backgroundColor: 'rgba(245,158,11,.6)', borderRadius: 4, stack: 'a' },
      ]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      onHover: (evt, els, chart) => {
        chart.canvas.style.cursor = els.length ? 'pointer' : 'default';
      },
      // Clique no nome (rótulo) ou na barra abre o menu individual do colaborador.
      onClick: (evt, els, chart) => {
        let idx = (els && els.length) ? els[0].index : null;
        if (idx == null && chart.scales.y) {
          const y = evt.y != null ? evt.y : (evt.native ? evt.native.offsetY : null);
          if (y != null) idx = Math.round(chart.scales.y.getValueForPixel(y));
        }
        if (idx == null || idx < 0 || idx >= sorted.length) return;
        const u = sorted[idx];
        const ev = evt.native || evt;
        if (typeof openPersonMenu === 'function') openPersonMenu(ev, u.user_id, displayName(u));
      },
      plugins: {
        legend: { labels: { color: ct.legend, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            title: (items) => items.length ? displayName(sorted[items[0].dataIndex]) : '',
            label: (ctx) => {
              const u   = sorted[ctx.dataIndex];
              const sec = ctx.datasetIndex === 0 ? (u.active || 0) : _taAfkOf(u);
              const nome = ctx.datasetIndex === 0 ? 'Ativo' : 'AFK na jornada';
              return ` ${nome}: ${fmtHM(sec)}`;
            }
          }
        }
      },
      scales: {
        x: { stacked: true, beginAtZero: true, ticks: { color: ct.label }, grid: { color: ct.grid } },
        y: { stacked: true, ticks: { color: ct.label, autoSkip: false }, grid: { display: false } },
      }
    }
  });
}
