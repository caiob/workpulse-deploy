// =====================================================================
// wp-jornada.js — Aba "Jornada de Trabalho" (dentro do menu Coletas)
//
// Interpreta os eventos de coleta como uma jornada de trabalho diária:
//   - Início da Jornada = primeiro evento de janela (window) do dia
//   - Final da Jornada  = último evento (ts + duração) do dia
//   - Pausas            = soma dos eventos AFK do dia (almoço, café, etc.)
//
// O dia atual aparece com Início preenchido, Final em branco (em andamento)
// e Pausas com o total acumulado até o momento.
//
// IMPORTANTE: como o endpoint /v1/events limita a 1000 eventos por chamada,
// fazemos uma busca por dia (em paralelo, com cap de concorrência) para
// garantir cobertura completa mesmo em períodos longos.
// =====================================================================

/* ---------- Helpers de formatação ---------- */
function _jorLocalDayKey(d) {
  const y  = d.getFullYear();
  const m  = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
}

function _jorHHMM(d) {
  if (!d) return '—';
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0');
}

function _jorDur(sec) {
  if (!sec || sec < 0) return '00:00';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}

function _jorDDMM(yyyy_mm_dd) {
  const [, m, d] = yyyy_mm_dd.split('-');
  return `${d}/${m}`;
}

function _jorWeekday(yyyy_mm_dd) {
  const [y, m, d] = yyyy_mm_dd.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  return ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'][dt.getDay()];
}

function _jorHourFloat(d) {
  return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
}

function _jorHHfromFloat(v) {
  if (v == null || isNaN(v)) return '—';
  const h = Math.floor(v);
  const m = Math.round((v - h) * 60);
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}

/* ---------- Fetch chunked por dia ---------- */
async function _jorFetchByDay(userId, days, bucket, signal) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const dayStarts = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    dayStarts.push(d);
  }
  const CONC = 5;
  const all  = [];
  for (let i = 0; i < dayStarts.length; i += CONC) {
    const batch = dayStarts.slice(i, i + CONC);
    const results = await Promise.all(batch.map(d => {
      const since = d.toISOString();
      const next  = new Date(d.getTime() + 86400000).toISOString();
      const params = new URLSearchParams({
        tenant:  state.tenant,
        user_id: userId,
        bucket:  bucket,
        since:   since,
        until:   next,
        limit:   '1000',
      });
      return api('/v1/events?' + params.toString(), signal).catch(() => ({ events: [] }));
    }));
    results.forEach(r => all.push(...(r.events || [])));
  }
  return all;
}

/* ---------- Núcleo: agrega eventos em jornadas diárias ---------- */
function computeJornadas(windowEvents, afkEvents) {
  const byDay = {};

  // 1) Window events definem início, fim e tempo focado
  for (const e of windowEvents) {
    if (!e.ts) continue;
    const start = new Date(e.ts);
    const dur   = e.duration_sec || 0;
    const end   = new Date(start.getTime() + dur * 1000);
    const key   = _jorLocalDayKey(start);
    if (!byDay[key]) {
      byDay[key] = { day: key, start: start, end: end, pausesSec: 0, focusSec: 0 };
    }
    if (start < byDay[key].start) byDay[key].start = start;
    if (end   > byDay[key].end)   byDay[key].end   = end;
    byDay[key].focusSec += dur;
  }

  // 2) AFK events dentro da janela viram pausas
  for (const e of afkEvents) {
    if (!e.ts) continue;
    const ts  = new Date(e.ts);
    const dur = e.duration_sec || 0;
    const key = _jorLocalDayKey(ts);
    if (!byDay[key]) continue; // dia sem atividade real (só AFK)
    // Considera AFK como pausa apenas se cair dentro da janela de trabalho
    const afkEnd = new Date(ts.getTime() + dur * 1000);
    if (afkEnd >= byDay[key].start && ts <= byDay[key].end) {
      byDay[key].pausesSec += dur;
    }
  }

  const todayKey = _jorLocalDayKey(new Date());
  return Object.values(byDay)
    .map(j => ({ ...j, isToday: j.day === todayKey }))
    .sort((a, b) => a.day.localeCompare(b.day));
}

/* ---------- Loader principal ---------- */
async function loadJornada() {
  await _populateUserDropdown('jorUser');
  const days = +document.getElementById('jorDays').value;
  const user = document.getElementById('jorUser').value.trim();
  const sig  = _tabAbort ? _tabAbort.signal : undefined;

  const tb = document.querySelector('#jornadaTable tbody');

  if (!user) {
    // Sem usuário selecionado, mostra estado vazio
    tb.innerHTML = `<tr><td colspan="6" class="empty-state">
      <h4>Selecione um colaborador</h4>
      Escolha alguém no seletor acima para ver a jornada de trabalho dia a dia.
    </td></tr>`;
    _jorResetKpis();
    if (state.charts.jorHours) { state.charts.jorHours.destroy(); state.charts.jorHours = null; }
    if (state.charts.jorTimes) { state.charts.jorTimes.destroy(); state.charts.jorTimes = null; }
    document.getElementById('jorUserLabel').textContent = '—';
    return;
  }

  try {
    const [windowEv, afkEv] = await Promise.all([
      _jorFetchByDay(user, days, 'window', sig),
      _jorFetchByDay(user, days, 'afk',    sig),
    ]);

    const jornadas = computeJornadas(windowEv, afkEv);

    // Nome amigável no rótulo
    const u = state.people.find(p => p.user_id === user) || { user_id: user };
    document.getElementById('jorUserLabel').textContent = displayName(u);

    _jorRenderKpis(jornadas);
    _jorDrawHoursChart(jornadas);
    _jorDrawTimesChart(jornadas);
    _jorRenderTable(jornadas);
  } catch (e) {
    toast('Erro ao carregar jornadas: ' + e.message);
  }
}

/* ---------- KPIs ---------- */
function _jorResetKpis() {
  ['kpiJorDays', 'kpiJorAvgWork', 'kpiJorAvgStart', 'kpiJorAvgEnd', 'kpiJorAvgPause'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '—';
  });
}

function _jorRenderKpis(jornadas) {
  const completos = jornadas.filter(j => !j.isToday);
  document.getElementById('kpiJorDays').innerHTML =
    jornadas.length + '<small>dias</small>';

  if (!completos.length) {
    document.getElementById('kpiJorAvgWork').innerHTML  = '—';
    document.getElementById('kpiJorAvgStart').innerHTML = '—';
    document.getElementById('kpiJorAvgEnd').innerHTML   = '—';
    document.getElementById('kpiJorAvgPause').innerHTML = '—';
    return;
  }

  const avgWorkSec  = completos.reduce((a, j) => a + Math.max(0, (j.end - j.start) / 1000 - j.pausesSec), 0) / completos.length;
  const avgPauseSec = completos.reduce((a, j) => a + j.pausesSec, 0) / completos.length;
  const avgStartMin = completos.reduce((a, j) => a + j.start.getHours() * 60 + j.start.getMinutes(), 0) / completos.length;
  const avgEndMin   = completos.reduce((a, j) => a + j.end.getHours()   * 60 + j.end.getMinutes(),   0) / completos.length;

  const sh = Math.floor(avgStartMin / 60), sm = Math.round(avgStartMin % 60);
  const eh = Math.floor(avgEndMin   / 60), em = Math.round(avgEndMin   % 60);

  document.getElementById('kpiJorAvgWork').innerHTML  = _jorDur(avgWorkSec)  + '<small>h</small>';
  document.getElementById('kpiJorAvgStart').innerHTML = String(sh).padStart(2, '0') + ':' + String(sm).padStart(2, '0');
  document.getElementById('kpiJorAvgEnd').innerHTML   = String(eh).padStart(2, '0') + ':' + String(em).padStart(2, '0');
  document.getElementById('kpiJorAvgPause').innerHTML = _jorDur(avgPauseSec) + '<small>h</small>';
}

/* ---------- Chart: horas líquidas + pausas por dia ---------- */
function _jorDrawHoursChart(jornadas) {
  if (state.charts.jorHours) state.charts.jorHours.destroy();
  if (!jornadas.length) return;
  const ct  = chartTheme();
  const ctx = document.getElementById('chartJorHours').getContext('2d');

  state.charts.jorHours = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: jornadas.map(j => _jorDDMM(j.day)),
      datasets: [
        {
          label: 'Tempo trabalhado',
          data: jornadas.map(j => {
            const gross = (j.end - j.start) / 1000;
            return +Math.max(0, (gross - j.pausesSec) / 3600).toFixed(2);
          }),
          backgroundColor: 'rgba(74,222,128,.80)',
          borderRadius: 6,
          stack: 'a',
        },
        {
          label: 'Pausas (AFK)',
          data: jornadas.map(j => +(j.pausesSec / 3600).toFixed(2)),
          backgroundColor: 'rgba(245,158,11,.70)',
          borderRadius: 6,
          stack: 'a',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend:  { labels: { color: ct.label, font: { size: 11 }, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: c => ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(1) + 'h',
            footer: items => {
              const total = items.reduce((s, i) => s + i.parsed.y, 0);
              return 'Total da jornada: ' + total.toFixed(1) + 'h';
            },
          },
        },
      },
      scales: {
        x: { stacked: true, ticks: { color: ct.label }, grid: { color: ct.grid } },
        y: { stacked: true, ticks: { color: ct.label, callback: v => v + 'h' }, grid: { color: ct.grid } },
      },
    },
  });
}

/* ---------- Chart: início × fim da jornada ---------- */
function _jorDrawTimesChart(jornadas) {
  if (state.charts.jorTimes) state.charts.jorTimes.destroy();
  if (!jornadas.length) return;
  const ct  = chartTheme();
  const ctx = document.getElementById('chartJorTimes').getContext('2d');

  // Min/max do eixo Y baseado nos dados (com margem)
  const startHours = jornadas.map(j => _jorHourFloat(j.start));
  const endHours   = jornadas.filter(j => !j.isToday).map(j => _jorHourFloat(j.end));
  const allH       = [...startHours, ...endHours];
  const yMin       = Math.max(0,  Math.floor(Math.min(...allH) - 1));
  const yMax       = Math.min(24, Math.ceil( Math.max(...allH) + 1));

  state.charts.jorTimes = new Chart(ctx, {
    type: 'line',
    data: {
      labels: jornadas.map(j => _jorDDMM(j.day)),
      datasets: [
        {
          label: 'Início',
          data: jornadas.map(j => +_jorHourFloat(j.start).toFixed(2)),
          borderColor: '#6c8cff',
          backgroundColor: 'rgba(108,140,255,.18)',
          tension: 0.35,
          pointRadius: 4,
          pointHoverRadius: 6,
          fill: false,
        },
        {
          label: 'Final',
          data: jornadas.map(j => j.isToday ? null : +_jorHourFloat(j.end).toFixed(2)),
          borderColor: '#f472b6',
          backgroundColor: 'rgba(244,114,182,.12)',
          tension: 0.35,
          pointRadius: 4,
          pointHoverRadius: 6,
          fill: false,
          spanGaps: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: ct.legend, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: c => ' ' + c.dataset.label + ': ' + _jorHHfromFloat(c.parsed.y),
          },
        },
      },
      scales: {
        x: { ticks: { color: ct.label }, grid: { display: false } },
        y: {
          min: yMin,
          max: yMax,
          ticks: { color: ct.label, stepSize: 1, callback: v => _jorHHfromFloat(v) },
          grid:  { color: ct.grid },
        },
      },
    },
  });
}

/* ---------- Tabela detalhada ---------- */
function _jorRenderTable(jornadas) {
  const tb = document.querySelector('#jornadaTable tbody');
  if (!jornadas.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty-state">Nenhuma jornada encontrada no período. Verifique se o agente está enviando eventos.</td></tr>';
    return;
  }
  // Mais recente primeiro
  const ordered = [...jornadas].reverse();

  tb.innerHTML = ordered.map(j => {
    const ddmm    = _jorDDMM(j.day);
    const wd      = _jorWeekday(j.day);
    const ini     = _jorHHMM(j.start);
    const grossSec = (j.end - j.start) / 1000;
    const liqSec   = Math.max(0, grossSec - j.pausesSec);
    const todayBadge = j.isToday
      ? ' <span class="pill" style="font-size:10px;padding:2px 8px;background:rgba(108,140,255,.18);color:var(--primary);border-color:rgba(108,140,255,.4)">HOJE</span>'
      : '';

    const fim = j.isToday
      ? '<span style="color:var(--text-muted);font-style:italic">em andamento</span>'
      : _jorHHMM(j.end);

    const liq = j.isToday
      ? '<span style="color:var(--text-muted);font-style:italic">parcial · ' + _jorDur(liqSec) + '</span>'
      : '<strong>' + _jorDur(liqSec) + '</strong>';

    return `<tr${j.isToday ? ' style="background:rgba(108,140,255,.05)"' : ''}>
      <td>
        <strong>${ddmm}</strong>
        <small style="color:var(--text-dim);margin-left:6px">${wd}</small>
        ${todayBadge}
      </td>
      <td><span style="font-family:var(--serif);font-size:15px;font-weight:600">${ini}</span></td>
      <td><span style="font-family:var(--serif);font-size:15px;font-weight:600">${fim}</span></td>
      <td>${_jorDur(j.pausesSec)}</td>
      <td style="color:var(--text-dim)">${j.isToday ? '—' : _jorDur(grossSec)}</td>
      <td>${liq}</td>
    </tr>`;
  }).join('');
}
