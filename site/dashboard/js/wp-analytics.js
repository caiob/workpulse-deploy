// =====================================================================
// wp-analytics.js — Abas "Alertas" e "Tendências"
// =====================================================================

/* -------------------- ALERTAS -------------------- */
const ALERT_RENDER = {
  afk_today:    p => `Ocioso <strong>${p.afk_minutes} min</strong> hoje no horário comercial.`,
  absent_today: p => `Ainda não iniciou a jornada${p.last_seen ? ` · visto ${relTime(p.last_seen)}` : ''}.`,
  risky_site:   p => `Acessou <strong>${escHtml(p.site)}</strong> (${p.minutes} min)${p.sample ? ` · <span style="color:var(--text-muted)">${escHtml(p.sample)}</span>` : ''}.`,
  overwork:     p => `Média de <strong>${p.avg_day_hours}h</strong> por dia útil.`,
  underwork:    p => `Média de <strong>${p.avg_day_hours}h</strong> por dia útil.`,
};

function _renderAlertList(elId, items, kind, emptyMsg, tone) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = (items && items.length)
    ? items.map(p => `
        <div class="alert-tile ${tone}">
          <div class="icon">${tone === 'over' ? '!' : '~'}</div>
          <div class="body">
            <div class="name">${escHtml(p.display_name || p.user_id)}</div>
            <div class="meta">${ALERT_RENDER[kind](p)}</div>
          </div>
        </div>`).join('')
    : `<div class="alert-tile empty">${emptyMsg}</div>`;
}

async function loadAlerts() {
  try {
    const { since, until } = wpDP.getRange('rangeDays');
    const d = await api(`/v1/alerts/today?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`);
    const a = d.alerts || {};
    const note = document.getElementById('alertsBizNote');
    if (note) {
      if (d.today_is_business === false) {
        note.style.display = 'block';
        note.textContent = 'Hoje não é dia útil (fim de semana ou feriado nacional) — os alertas de AFK e ausência do dia ficam suspensos.';
      } else { note.style.display = 'none'; }
    }
    _renderAlertList('afkTodayList',    a.afk_today,    'afk_today',    'Nenhum excesso de AFK hoje.',                 'under');
    _renderAlertList('absentTodayList', a.absent_today, 'absent_today', 'Ninguém ausente até agora.',                  'under');
    _renderAlertList('riskyList',       a.risky_site,   'risky_site',   'Nenhum acesso de risco hoje.',                'over');
    _renderAlertList('overworkList',    a.overwork,     'overwork',     'Nenhum sinal de sobrecarga no período.',      'over');
    _renderAlertList('underworkList',   a.underwork,    'underwork',    'Nenhum sinal de baixo engajamento no período.', 'under');
  } catch (e) { toast('Erro: ' + e.message); }
}

/* -------------------- CONFIG DE ALERTAS (Administrativo) -------------------- */
const ALERT_TYPE_ORDER = ['afk_today', 'absent_today', 'risky_site', 'underwork', 'overwork'];

async function loadAlertConfig() {
  try {
    const d = await api('/v1/alerts/config?tenant=' + encodeURIComponent(state.tenant));
    const s = d.settings || {};
    document.getElementById('alBStart').value   = s.business_start || '08:00';
    document.getElementById('alBEnd').value     = s.business_end   || '18:00';
    document.getElementById('alCutoff').value   = s.absent_cutoff  || '10:00';
    document.getElementById('alAfkMin').value   = s.afk_threshold_min ?? 120;
    document.getElementById('alUnderMax').value = s.underwork_max_h ?? 2;
    document.getElementById('alOverMin').value  = s.overwork_min_h ?? 10;
    _renderAlertRules(d.rules || {}, d.alert_labels || {});
    _renderRiskySites(d.risky_sites || []);
  } catch (e) { toast('Erro ao carregar alertas: ' + e.message); }
}

function _renderAlertRules(rules, labels) {
  const box = document.getElementById('alertRulesBox');
  if (!box) return;
  box.innerHTML = ALERT_TYPE_ORDER.map(t => {
    const r = rules[t] || { enabled: true, email_enabled: false, recipients: [] };
    const recips = (r.recipients || []).join(', ');
    return `<div style="border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:10px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
        <strong>${escHtml(labels[t] || t)}</strong>
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          <label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type="checkbox" id="ar_en_${t}" ${r.enabled ? 'checked' : ''} style="width:auto"> Ativo</label>
          <label style="display:inline-flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type="checkbox" id="ar_em_${t}" ${r.email_enabled ? 'checked' : ''} style="width:auto"> Enviar e-mail</label>
        </div>
      </div>
      <div style="margin-top:8px">
        <label style="font-size:12px;color:var(--text-dim)">Destinatários do e-mail (separe por vírgula)</label>
        <input id="ar_rc_${t}" value="${escHtml(recips)}" placeholder="focal@empresa.com" style="width:100%;box-sizing:border-box">
      </div>
    </div>`;
  }).join('');
}

async function saveAlertConfig() {
  const rules = ALERT_TYPE_ORDER.map(t => ({
    alert_type:    t,
    enabled:       document.getElementById('ar_en_' + t).checked,
    email_enabled: document.getElementById('ar_em_' + t).checked,
    recipients:    document.getElementById('ar_rc_' + t).value.split(',').map(s => s.trim()).filter(s => s.includes('@')),
  }));
  const body = {
    business_start:    document.getElementById('alBStart').value || '08:00',
    business_end:      document.getElementById('alBEnd').value   || '18:00',
    absent_cutoff:     document.getElementById('alCutoff').value || '10:00',
    afk_threshold_min: parseInt(document.getElementById('alAfkMin').value, 10) || 120,
    underwork_max_h:   parseFloat(document.getElementById('alUnderMax').value) || 2,
    overwork_min_h:    parseFloat(document.getElementById('alOverMin').value) || 10,
    rules,
  };
  const st = document.getElementById('alStatus');
  try {
    await apiFetch('PUT', '/v1/alerts/config?tenant=' + encodeURIComponent(state.tenant), body);
    if (st) { st.textContent = 'Salvo.'; st.style.color = '#4ade80'; setTimeout(() => st.textContent = '', 2500); }
    toast('Configurações de alerta salvas.');
  } catch (e) { if (st) { st.textContent = 'Falha: ' + e.message; st.style.color = 'var(--danger)'; } }
}

let _riskyAll = [];
let _riskyPage = 0;
const RISKY_PER_PAGE = 10;

function _renderRiskySites(list) {
  _riskyAll = list || [];
  _riskyPage = 0;
  _riskyDraw();
}

function _riskyFiltered() {
  const q = (document.getElementById('rsFilter')?.value || '').trim().toLowerCase();
  if (!q) return _riskyAll;
  return _riskyAll.filter(s =>
    (s.pattern || '').toLowerCase().includes(q) || (s.label || '').toLowerCase().includes(q));
}

function riskyFilter() { _riskyPage = 0; _riskyDraw(); }

function _riskyDraw() {
  const box = document.getElementById('riskySitesBox');
  if (!box) return;
  const list = _riskyFiltered();
  const total = list.length;
  if (!total) {
    const filtering = !!(document.getElementById('rsFilter')?.value || '').trim();
    box.innerHTML = `<p class="empty-state">${filtering ? 'Nenhum site corresponde à busca.' : 'Nenhum site na lista.'}</p>`;
    return;
  }
  const pages = Math.ceil(total / RISKY_PER_PAGE);
  if (_riskyPage >= pages) _riskyPage = pages - 1;
  if (_riskyPage < 0) _riskyPage = 0;
  const start = _riskyPage * RISKY_PER_PAGE;
  const slice = list.slice(start, start + RISKY_PER_PAGE);
  const rows = slice.map(s => `<tr>
      <td><code>${escHtml(s.pattern)}</code></td>
      <td>${escHtml(s.label || '—')}</td>
      <td><button class="ghost" onclick="delRiskySite(${s.id})">remover</button></td>
    </tr>`).join('');
  let pager = '';
  if (pages > 1) {
    pager = `<div style="display:flex;align-items:center;gap:12px;justify-content:flex-end;margin-top:10px;font-size:13px">
      <button class="ghost" ${_riskyPage === 0 ? 'disabled' : ''} onclick="riskyPage(-1)">‹ Anterior</button>
      <span style="color:var(--text-dim)">${start + 1}–${Math.min(start + RISKY_PER_PAGE, total)} de ${total}</span>
      <button class="ghost" ${_riskyPage >= pages - 1 ? 'disabled' : ''} onclick="riskyPage(1)">Próxima ›</button>
    </div>`;
  }
  box.innerHTML = '<table><thead><tr><th>Padrão</th><th>Rótulo</th><th></th></tr></thead><tbody>' +
    rows + '</tbody></table>' + pager;
}

function riskyPage(delta) { _riskyPage += delta; _riskyDraw(); }

async function addRiskySite() {
  const pattern = document.getElementById('rsPattern').value.trim();
  const label   = document.getElementById('rsLabel').value.trim();
  if (!pattern) { toast('Informe o padrão do site.'); return; }
  try {
    await apiFetch('POST', '/v1/alerts/risky-sites?tenant=' + encodeURIComponent(state.tenant), { pattern, label });
    document.getElementById('rsPattern').value = '';
    document.getElementById('rsLabel').value   = '';
    toast('Site adicionado.');
    loadAlertConfig();
  } catch (e) { toast('Falha: ' + e.message); }
}

async function delRiskySite(id) {
  try {
    await apiFetch('DELETE', '/v1/alerts/risky-sites/' + id + '?tenant=' + encodeURIComponent(state.tenant));
    loadAlertConfig();
  } catch (e) { toast('Falha: ' + e.message); }
}

async function bulkImportRiskySites() {
  const raw = document.getElementById('rsBulk').value || '';
  const items = raw.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
    const i = l.search(/[,;]/);
    return i >= 0
      ? { pattern: l.slice(0, i).trim(), label: l.slice(i + 1).trim() }
      : { pattern: l, label: '' };
  }).filter(x => x.pattern);
  if (!items.length) { toast('Cole ao menos uma linha.'); return; }
  try {
    const r = await apiFetch('POST', '/v1/alerts/risky-sites/bulk?tenant=' + encodeURIComponent(state.tenant), { items });
    toast(`Importados: ${r.added || 0} · ignorados (já existiam): ${r.skipped || 0}.`);
    document.getElementById('rsBulk').value = '';
    loadAlertConfig();
  } catch (e) { toast('Falha: ' + e.message); }
}

/* -------------------- TENDÊNCIAS -------------------- */
async function loadTrends() {
  try {
    const { since: tSince, until: tUntil } = wpDP.getRange('trendDays');
    const [active, afk] = await Promise.all([
      api(`/v1/workforce/timeseries?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(tSince)}&until=${encodeURIComponent(tUntil)}&bucket=window`),
      api(`/v1/workforce/timeseries?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(tSince)}&until=${encodeURIComponent(tUntil)}&bucket=afk`),
    ]);
    drawTrend('chartTrendActive', active.series, '#6c8cff', 'Tempo ativo');
    drawTrend('chartTrendAfk',    afk.series,    '#f59e0b', 'AFK');
  } catch (e) { toast('Erro nas tendências: ' + e.message); }
}

function drawTrend(canvasId, series, color, label) {
  const ct = chartTheme();
  if (state.charts[canvasId]) state.charts[canvasId].destroy();
  state.charts[canvasId] = new Chart(document.getElementById(canvasId).getContext('2d'), {
    type: 'line',
    data: {
      labels: series.map(s => ptDate(s.day)),
      datasets: [{
        label:           label + ' (h)',
        data:            series.map(s => +(s.seconds / 3600).toFixed(2)),
        borderColor:     color,
        backgroundColor: color + '22',
        fill:            true,
        tension:         0.35,
        pointRadius:     3,
        pointBackgroundColor: color,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: ct.legend } } },
      scales: {
        x: { ticks: { color: ct.label }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: ct.label }, grid: { color: ct.grid } }
      }
    }
  });
}
