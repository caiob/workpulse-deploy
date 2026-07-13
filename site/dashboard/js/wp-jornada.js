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
// CÁLCULO: feito preferencialmente no SERVIDOR (endpoint /v1/jornada), via
// agregação SQL por dia LOCAL e SEM limite de linhas. Se o servidor ainda
// não tiver esse endpoint (deploy do backend pendente), cai num fallback
// que pagina TODOS os eventos do /v1/events — evitando o "corte" do limite
// de 1000 que deslocava o início (ex.: mostrava 16:01 em vez de 08:01).
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

/* ---------- Converte resposta do endpoint /v1/jornada em jornadas ----------
 * As datas são reconstruídas a partir do horário LOCAL já calculado pelo
 * servidor (start_float/end_float = horas desde a meia-noite local), de
 * forma que a exibição não dependa do fuso do navegador.
 */
function _jorLunchSec() {
  return ((typeof state !== 'undefined' && state.lunchMin != null) ? state.lunchMin : 60) * 60;
}

function _jorBuildJornada(d) {
  const [y, m, dd] = d.day.split('-').map(Number);
  const base  = new Date(y, m - 1, dd, 0, 0, 0, 0);        // meia-noite local
  const start = new Date(base.getTime() + (d.start_float || 0) * 3600000);
  const end   = new Date(base.getTime() + (d.end_float   || 0) * 3600000);
  // O almoço agora é descontado NO SERVIDOR (pauses_sec já vem líquido) e a
  // Líquida (net_sec) também já desconta suspensão (sleep_sec) e buracos sem
  // coleta (gap_sec). O desconto client-side foi REMOVIDO — era aplicado em
  // duplicidade com outras telas (ver ANALISE_CONFLITO_SARA_0607.md).
  // Fallback: servidor antigo (sem gap_sec) → desconta o almoço aqui como antes.
  const legacy = (d.gap_sec == null);
  let pauses = d.pauses_sec || 0;
  if (legacy) {
    const _ls = _jorLunchSec();
    pauses = pauses >= _ls ? (pauses - _ls) : pauses;
  }
  const gross = Math.max(0, (end - start) / 1000);
  const uncov = (d.sleep_sec || 0) + (d.gap_sec || 0);   // sem coleta na jornada
  const net   = legacy ? Math.max(0, gross - pauses) : (d.net_sec || 0);
  return {
    day:       d.day,
    start:     start,
    end:       end,
    pausesSec: pauses,
    uncovSec:  uncov,
    netSec:    net,
    coveragePct: (d.coverage_pct != null) ? d.coverage_pct : null,
    focusSec:  d.focus_sec  || 0,
    idleAfterSec: d.idle_after_sec || 0,
    isToday:   !!d.is_today,
  };
}

/* ---------- Caminho A: endpoint agregado no servidor (/v1/jornada) ---------- */
async function _jorViaEndpoint(user, jorSince, jorUntil, sig) {
  const params = new URLSearchParams({
    tenant:  state.tenant,
    user_id: user,
    since:   new Date(jorSince).toISOString(),
    until:   new Date(jorUntil).toISOString(),
  });
  const resp = await api('/v1/jornada?' + params.toString(), sig);
  return (resp.days || [])
    .map(_jorBuildJornada)
    .sort((a, b) => a.day.localeCompare(b.day));
}

/* ---------- Caminho B (fallback): /v1/events com PAGINAÇÃO completa ----------
 * Usado quando o servidor ainda não tem o /v1/jornada. Pagina TODOS os
 * eventos de cada dia (offset += 1000) para não sofrer o "corte" do limite.
 */
async function _jorFetchAllByDay(userId, dayStarts, bucket, signal) {
  const CONC = 4;
  const all  = [];
  for (let i = 0; i < dayStarts.length; i += CONC) {
    const batch = dayStarts.slice(i, i + CONC);
    const results = await Promise.all(batch.map(async d => {
      const since = d.toISOString();
      const next  = new Date(d.getTime() + 86400000).toISOString();
      const dayEvents = [];
      let offset = 0;
      for (let guard = 0; guard < 100; guard++) {       // teto de segurança
        const params = new URLSearchParams({
          tenant:  state.tenant,
          user_id: userId,
          bucket:  bucket,
          since:   since,
          until:   next,
          limit:   '1000',
          offset:  String(offset),
          order:   'asc',
        });
        let r;
        try { r = await api('/v1/events?' + params.toString(), signal); }
        catch { break; }
        const evs = r.events || [];
        dayEvents.push(...evs);
        if (evs.length < 1000) break;                   // última página
        offset += 1000;
      }
      return dayEvents;
    }));
    results.forEach(evs => all.push(...evs));
  }
  return all;
}

/* Agrega eventos crus em jornadas diárias (usado só no fallback) */
function _jorComputeFromEvents(windowEvents, afkEvents) {
  const byDay = {};
  for (const e of windowEvents) {
    if (!e.ts) continue;
    const start = new Date(e.ts);
    const dur   = e.duration_sec || 0;
    const end   = new Date(start.getTime() + dur * 1000);
    const key   = _jorLocalDayKey(start);
    if (!byDay[key]) byDay[key] = { day: key, start, end, pausesSec: 0, focusSec: 0, idleAfterSec: 0 };
    if (start < byDay[key].start) byDay[key].start = start;
    if (end   > byDay[key].end)   byDay[key].end   = end;
    byDay[key].focusSec += dur;
  }
  for (const e of afkEvents) {
    if (!e.ts) continue;
    const status = (e.data && e.data.status) || (e.data_json && e.data_json.status);
    if (status && status !== 'afk') continue;           // só ociosidade real
    const ts  = new Date(e.ts);
    const dur = e.duration_sec || 0;
    const key = _jorLocalDayKey(ts);
    if (!byDay[key]) continue;
    const afkEnd = new Date(ts.getTime() + dur * 1000);
    if (afkEnd >= byDay[key].start && ts <= byDay[key].end) byDay[key].pausesSec += dur;
  }
  const todayKey = _jorLocalDayKey(new Date());
  // Desconta o almoço (config dos Alertas, padrão 60 min) das Pausas, POR DIA,
  // capado em 0 → a Líquida sobe pelo valor perdoado. Consistente com as outras telas.
  const lunchSec = _jorLunchSec();
  return Object.values(byDay)
    .map(j => {
      const pauses = (j.pausesSec >= lunchSec ? j.pausesSec - lunchSec : j.pausesSec);
      const gross  = Math.max(0, (j.end - j.start) / 1000);
      // Fallback sem servidor: não detecta buracos de coleta (uncov = 0).
      return { ...j, pausesSec: pauses, uncovSec: 0, netSec: Math.max(0, gross - pauses),
               coveragePct: null, isToday: j.day === todayKey };
    })
    .sort((a, b) => a.day.localeCompare(b.day));
}

async function _jorViaEvents(user, jorSince, jorUntil, sig) {
  const jorStart = new Date(jorSince); jorStart.setHours(0, 0, 0, 0);
  const jorEnd   = new Date(jorUntil); jorEnd.setHours(0, 0, 0, 0);
  const dayStarts = [];
  for (let d = new Date(jorEnd); d >= jorStart; d.setDate(d.getDate() - 1))
    dayStarts.push(new Date(d));
  const [windowEv, afkEv] = await Promise.all([
    _jorFetchAllByDay(user, dayStarts, 'window', sig),
    _jorFetchAllByDay(user, dayStarts, 'afk',    sig),
  ]);
  return _jorComputeFromEvents(windowEv, afkEv);
}

/* ---------- Loader principal ---------- */
async function loadJornada() {
  await _populateUserDropdown('jorUser');
  if (typeof _applyDeepLinkUser === 'function') _applyDeepLinkUser('jorUser');
  const { since: jorSince, until: jorUntil } = wpDP.getRange('jorDays');
  const user = document.getElementById('jorUser').value.trim();
  const sig  = _tabAbort ? _tabAbort.signal : undefined;

  const tb = document.querySelector('#jornadaTable tbody');

  if (!user) {
    // Sem usuário selecionado, mostra estado vazio
    tb.innerHTML = `<tr><td colspan="7" class="empty-state">
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
    let jornadas;
    try {
      jornadas = await _jorViaEndpoint(user, jorSince, jorUntil, sig);   // preferido
    } catch (err) {
      if (/^404/.test(err.message || '')) {
        jornadas = await _jorViaEvents(user, jorSince, jorUntil, sig);   // fallback
      } else {
        throw err;
      }
    }

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

  const avgWorkSec  = completos.reduce((a, j) => a + (j.netSec != null ? j.netSec : Math.max(0, (j.end - j.start) / 1000 - j.pausesSec)), 0) / completos.length;
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
          data: jornadas.map(j => +Math.max(0, (j.netSec || 0) / 3600).toFixed(2)),
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
        {
          // Suspensão da máquina + trechos sem nenhum evento (> 5 min).
          // Antes era somado ao verde como se fosse trabalho.
          label: 'Sem coleta',
          data: jornadas.map(j => +((j.uncovSec || 0) / 3600).toFixed(2)),
          backgroundColor: 'rgba(148,163,184,.45)',
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
    tb.innerHTML = '<tr><td colspan="7" class="empty-state">Nenhuma jornada encontrada no período. Verifique se o agente está enviando eventos.</td></tr>';
    return;
  }
  // Mais recente primeiro
  const ordered = [...jornadas].reverse();

  tb.innerHTML = ordered.map(j => {
    const ddmm    = _jorDDMM(j.day);
    const wd      = _jorWeekday(j.day);
    const ini     = _jorHHMM(j.start);
    const grossSec = (j.end - j.start) / 1000;
    const liqSec   = (j.netSec != null) ? j.netSec : Math.max(0, grossSec - j.pausesSec);
    const uncovTag = (j.uncovSec || 0) > 300
      ? ' <small style="color:var(--text-dim)" title="Tempo dentro da jornada sem nenhuma coleta (suspensão / agente parado) — já descontado da Líquida">sem coleta ' + _jorDur(j.uncovSec) + '</small>'
      : '';
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
      <td>${j.idleAfterSec > 0 ? _jorDur(j.idleAfterSec) : '<span style="color:var(--text-dim)">—</span>'}</td>
      <td style="color:var(--text-dim)">${j.isToday ? '—' : _jorDur(grossSec)}</td>
      <td>${liq}${uncovTag}</td>
    </tr>`;
  }).join('');
}
