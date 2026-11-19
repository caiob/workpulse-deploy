// =====================================================================
// wp-behavior.js — Aba "Comportamento" (Análise de Comportamento)
//
// Velocidade: a página NÃO consulta o banco a cada clique. Ela baixa UMA vez o
// pacote pronto (/v1/behavior/pack — gerado por job no servidor, gzip + ETag,
// ~150 KB) e faz filtro, ordenação, gráficos e ficha tudo no navegador.
// Trocar de aba e voltar reaproveita o pacote em memória.
// Gráficos em SVG puro (sem dependência).
// =====================================================================

const BH = {
  days: 15, pack: null, packKey: '', loading: false, pollTimer: null,
  dept: '', sel: null, q: '', onlyHigh: false, sigType: '', sigSev: '', heatMode: 'ativo',
};
const BH_SEV_LABEL = { alta: 'Alta', media: 'Média', baixa: 'Baixa' };
const BH_SEV_ORD = { alta: 0, media: 1, baixa: 2 };
const BH_DOW = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'];
const BH_ENG_CUT = 70;
const BH_SVGNS = 'http://www.w3.org/2000/svg';

/* ---------------------------------------------------------------- utilidades */
const bhNum = v => (v == null || v === '') ? 0 : +v;
const bhTruthy = v => v === true || v === 'true' || v === 1;
function bhHm(m) { m = Math.round(bhNum(m)); return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0'); }
function bhDia(s) { if (!s || s === 'período') return 'Período'; const d = new Date(s + 'T12:00:00'); return BH_DOW[d.getDay()] + ' ' + String(d.getDate()).padStart(2, '0') + '/' + String(d.getMonth() + 1).padStart(2, '0'); }
function bhToMin(c) { if (!c) return null; const p = String(c).split(':'); return (+p[0]) * 60 + (+p[1]); }
function bhPct(v) { return String(bhNum(v)).replace('.', ',') + '%'; }
function bhEl(tag, cls, txt) { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
function bhSvg(tag, attrs, parent) {
  const e = document.createElementNS(BH_SVGNS, tag);
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function bhId(id) { return document.getElementById(id); }
const bhCss = name => getComputedStyle(document.body).getPropertyValue(name).trim();
let _bhCtx = null;
function bhTextW(t, size, weight) {
  if (!_bhCtx) _bhCtx = document.createElement('canvas').getContext('2d');
  _bhCtx.font = (weight || 400) + ' ' + (size || 12) + 'px Inter, -apple-system, sans-serif';
  return _bhCtx.measureText(t).width;
}
function bhFit(t, w, size, weight) {   // corta o rótulo com "…" para caber na largura
  t = String(t || ''); if (bhTextW(t, size, weight) <= w) return t;
  while (t.length > 1 && bhTextW(t + '…', size, weight) > w) t = t.slice(0, -1);
  return t + '…';
}
function bhInDept(d) { return !BH.dept || d === BH.dept; }
function bhRows(name) { return (BH.pack && BH.pack[name]) || []; }
function bhSevPill(sev, txt) { return bhEl('span', 'pill bh-sev-' + sev, txt != null ? txt : BH_SEV_LABEL[sev]); }

/* tooltip único, posicionado no mouse */
function bhTip() {
  let t = bhId('bhTip');
  if (!t) { t = bhEl('div', 'bh-tip'); t.id = 'bhTip'; document.body.appendChild(t); }
  return t;
}
function bhShowTip(ev, parts) {
  const t = bhTip(); t.replaceChildren();
  parts.filter(Boolean).forEach((p, i) => t.appendChild(bhEl(i === 0 ? 'b' : 'div', i === 0 ? '' : 'bh-tip-sub', p)));
  t.style.opacity = 1;
  const w = t.offsetWidth, h = t.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  t.style.left = x + 'px'; t.style.top = y + 'px';
}
function bhHideTip() { const t = bhId('bhTip'); if (t) t.style.opacity = 0; }
function bhHover(el, parts) {
  el.addEventListener('mousemove', ev => bhShowTip(ev, typeof parts === 'function' ? parts() : parts));
  el.addEventListener('mouseleave', bhHideTip);
}
function bhEmpty(node, txt) { node.replaceChildren(bhEl('div', 'bh-empty', txt)); }
function bhTbodyMsg(tb, cols, txt) { const tr = bhEl('tr'); const td = bhEl('td', 'bh-empty', txt); td.colSpan = cols; tr.appendChild(td); tb.replaceChildren(tr); }

/* ---------------------------------------------------------------- carga */
async function loadBehavior(force) {
  const key = state.tenant + '|' + BH.days;
  if (!force && BH.pack && BH.packKey === key) { bhRenderAll(); return; }   // instantâneo: já em memória
  if (BH.loading) return;
  BH.loading = true; bhStatus('Carregando…');
  clearTimeout(BH.pollTimer);
  try {
    const qs = `tenant=${encodeURIComponent(state.tenant)}&days=${BH.days}`;
    const data = await api(`/v1/behavior/pack?${qs}`);
    if (data && data.building) {
      bhStatus('Montando a análise deste período pela primeira vez… (cerca de 1 minuto)');
      BH.pollTimer = setTimeout(() => loadBehavior(true), 15000);
      return;
    }
    BH.pack = data; BH.packKey = key;
    bhRenderAll();
  } catch (e) {
    bhStatus('');
    toast('Erro ao carregar comportamento: ' + e.message);
  } finally {
    BH.loading = false;
  }
}

async function bhRebuild() {
  try {
    await apiFetch('POST', `/v1/behavior/pack/rebuild?tenant=${encodeURIComponent(state.tenant)}&days=${BH.days}`);
    toast('Recalculando a análise com os dados e regras atuais…');
    bhStatus('Recalculando… a página atualiza sozinha quando terminar.');
    const before = BH.pack && BH.pack.meta && BH.pack.meta.gerado_em;
    let tries = 0;
    const poll = async () => {
      tries++;
      try {
        const d = await api(`/v1/behavior/pack?tenant=${encodeURIComponent(state.tenant)}&days=${BH.days}`);
        if (d && !d.building && d.meta && d.meta.gerado_em !== before) {
          BH.pack = d; BH.packKey = state.tenant + '|' + BH.days; bhRenderAll(); toast('Análise atualizada.'); return;
        }
      } catch (e) { /* tenta de novo */ }
      if (tries < 20) BH.pollTimer = setTimeout(poll, 10000); else bhStatus('');
    };
    BH.pollTimer = setTimeout(poll, 8000);
  } catch (e) { toast('Erro ao recalcular: ' + e.message); }
}

function bhStatus(txt) {
  const el = bhId('bhStatus'); if (!el) return;
  if (txt) { el.textContent = txt; return; }
  const m = BH.pack && BH.pack.meta;
  if (!m) { el.textContent = ''; return; }
  const ago = Math.max(0, Math.round((Date.now() - new Date(m.gerado_em).getTime()) / 60000));
  el.textContent = `Atualizado há ${ago < 60 ? ago + ' min' : bhHm(ago)} · ${m.pessoas} colaboradores · ${m.sinais} sinais`;
}

function bhSetDays(d) {
  BH.days = d;
  document.querySelectorAll('.bh-days button').forEach(b => b.classList.toggle('on', +b.dataset.days === d));
  loadBehavior();
}

/* ---------------------------------------------------------------- render geral */
function bhRenderAll() {
  if (!BH.pack) return;
  bhStatus();
  bhPeriodo(); bhDeptSelect(); bhKpis();
  bhRenderFiltered();
  bhRenderPerson();
}
function bhSyncHeights() {
  document.querySelectorAll('#tab-behavior .bh-pair').forEach(g => {
    const card = g.querySelector('.card'), tw = g.querySelector('.bh-tablewrap');
    if (!card || !tw) return;
    tw.style.maxHeight = ''; tw.style.height = '';
    const h = card.offsetHeight;
    if (h > 160) { tw.style.maxHeight = h + 'px'; tw.style.height = h + 'px'; }
  });
}
function bhRenderFiltered() {
  bhEngChart(); bhEngTab(); bhHolChart(); bhHolTab(); bhStaChart(); bhStaTab(); bhNigChart(); bhNigTab(); bhSecChart(); bhSecTab(); bhSocChart(); bhWaTab(); bhHibChart(); bhHibTab();
  bhTipos(); bhHeat(); bhScatter(); bhRanking(); bhSigTipoSelect(); bhSinaisTab();
  requestAnimationFrame(bhSyncHeights);
}
function bhPeriodo() {
  const m = BH.pack.meta, f = s => s ? s.split('-').reverse().join('/') : '—';
  bhId('bhPeriodo').textContent = f(m.periodo[0]) + ' a ' + f(m.periodo[1]);
}
function bhDeptSelect() {
  const sel = bhId('bhDept'); const c = {};
  bhRows('pessoas').forEach(p => { c[p.depto] = (c[p.depto] || 0) + 1; });
  sel.replaceChildren(new Option('Todos os departamentos', ''));
  Object.keys(c).sort((a, b) => c[b] - c[a]).forEach(d => sel.appendChild(new Option(d + ' (' + c[d] + ')', d)));
  if (BH.dept && !c[BH.dept]) BH.dept = '';
  sel.value = BH.dept;
}

/* ---------------------------------------------------------------- KPIs */
function bhKpis() {
  const P = bhRows('pessoas'), S = bhRows('sinais'), D = bhRows('dias');
  const pessoasCom = tipo => new Set(S.filter(x => x.tipo === tipo).map(x => x.user_id)).size;
  const hollow = {}; S.forEach(x => { if (x.tipo === 'hollow_day') hollow[x.user_id] = (hollow[x.user_id] || 0) + 1; });
  const uteis = D.filter(d => bhTruthy(d.trabalhou) && !bhTruthy(d.fim_de_semana_feriado));
  let act = 0, nw = 0; uteis.forEach(d => { act += bhNum(d.ativo_min); nw += bhNum(d.nao_trabalho_min); });
  const k = {
    colaboradores: P.length,
    pessoas_baixa: pessoasCom('low_activity'),
    pessoas_pouco_ativo: Object.keys(hollow).filter(u => hollow[u] >= 2).length,
    risco_alto: P.filter(p => bhNum(p.risco) >= 50).length,
    risco_medio: P.filter(p => bhNum(p.risco) >= 20).length,
    social_30: P.filter(p => bhNum(p.social_min_dia) >= 30).length,
    pessoas_ferias: pessoasCom('vacation_activity'),
    ativo_medio: bhHm(uteis.length ? act / uteis.length : 0),
    nao_trabalho: (act ? (100 * nw / act) : 0).toFixed(1).replace('.', ',') + '%',
  };
  document.querySelectorAll('#tab-behavior [data-bh-kpi]').forEach(el => {
    const v = k[el.dataset.bhKpi]; el.textContent = typeof v === 'number' ? v.toLocaleString('pt-BR') : v;
  });
}

/* ---------------------------------------------------------------- barras horizontais */
function bhHbars(node, L, opt) {
  node.replaceChildren();
  if (!L.length) { bhEmpty(node, opt.empty || 'Nada neste filtro.'); return; }
  const refs = opt.refs || [];
  const W = Math.max(300, node.clientWidth || 600), rowH = 28, H = L.length * rowH + (refs.length ? 24 : 6);
  let lw = 0, tw = 0;
  L.forEach(d => { lw = Math.max(lw, bhTextW(d.label, 12, 500)); tw = Math.max(tw, bhTextW(d.text, 12)); });
  lw = Math.min(lw + 14, W * 0.42);
  const mx = Math.max(Math.max(...L.map(d => d.v)) || 1, opt.max || 0, ...refs.map(r => r.v));
  const x0 = lw, x1 = Math.max(lw + 40, W - tw - 14);
  const X = v => x0 + (x1 - x0) * (v / mx);
  const svg = bhSvg('svg', { width: '100%', height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opt.aria || '' }, node);
  refs.forEach((r, i) => {   // rótulos para fora (1º à esquerda da linha, 2º à direita) para não se sobreporem
    const side = refs.length > 1 ? (i === 0 ? -1 : 1) : 0;
    bhSvg('line', { x1: X(r.v), x2: X(r.v), y1: 0, y2: L.length * rowH + 6, stroke: r.color, 'stroke-dasharray': '4 3', 'stroke-width': 1.2 }, svg);
    bhSvg('text', { x: X(r.v) + side * 4, y: L.length * rowH + 18, 'text-anchor': side < 0 ? 'end' : (side > 0 ? 'start' : 'middle'), class: 'bh-lbl', style: 'fill:' + r.color }, svg).textContent = r.label;
  });
  L.forEach((d, i) => {
    const y = i * rowH + 2, g = bhSvg('g', { class: d.id ? 'bh-click' : '' }, svg);
    bhSvg('rect', { x: 0, y, width: W, height: rowH - 2, fill: 'transparent' }, g);
    bhSvg('text', { x: lw - 10, y: y + 17, 'text-anchor': 'end', class: 'bh-lbl-strong' }, g).textContent = bhFit(d.label, lw - 14, 12, 500);
    bhSvg('rect', { x: x0, y: y + 6, width: Math.max(1, X(d.v) - x0), height: rowH - 13, rx: 3, fill: d.color || 'var(--primary)' }, g);
    bhSvg('text', { x: X(d.v) + 7, y: y + 17, class: 'bh-lbl' }, g).textContent = d.text;
    if (d.tip) bhHover(g, d.tip);
    if (d.id) g.addEventListener('click', () => bhSelectPerson(d.id, true));
  });
}

/* ---------------------------------------------------------------- Pouco tempo logado */
function bhCounts() {
  const c = {};
  bhRows('sinais').forEach(s => {
    const k = c[s.user_id] || (c[s.user_id] = { hollow: 0, absent: 0 });
    if (s.tipo === 'hollow_day') k.hollow++;
    if (s.tipo === 'absent_days') { const m = String(s.descricao || '').match(/^(\d+)/); k.absent = m ? +m[1] : 1; }
  });
  return c;
}
function bhLowRows() {
  const c = bhCounts();
  return bhRows('pessoas').filter(p => bhInDept(p.depto) && p.engajamento != null && p.engajamento !== '')
    .map(p => ({ p, e: bhNum(p.engajamento), c: c[p.user_id] || { hollow: 0, absent: 0 } }))
    .filter(r => r.e < BH_ENG_CUT).sort((a, b) => a.e - b.e);
}
const bhEngColor = e => e < 45 ? 'var(--danger)' : (e < 60 ? 'var(--warn)' : 'var(--primary)');
const bhEngSev = e => e < 45 ? 'alta' : (e < 60 ? 'media' : 'baixa');
function bhEngChart() {
  const L = bhLowRows().map(r => ({
    id: r.p.user_id, label: r.p.nome, v: r.e, color: bhEngColor(r.e), text: r.e + '% · ' + bhHm(r.p.ativo_medio_min) + '/dia',
    tip: [r.p.nome, r.p.depto + (r.p.cargo ? ' · ' + r.p.cargo : ''), bhHm(r.p.ativo_medio_min) + ' ativo por dia útil; mediana do depto ' + bhHm(r.p.ref_depto_min),
      r.c.hollow + ' dia(s) presente mas pouco ativo · ' + r.c.absent + ' dia(s) útil(eis) sem atividade'],
  }));
  bhHbars(bhId('bhEngChart'), L, {
    aria: 'Colaboradores abaixo de 70% da mediana do departamento', max: BH_ENG_CUT, empty: 'Ninguém abaixo de 70% do departamento neste filtro.',
    refs: [{ v: 45, label: '45% · alta', color: 'var(--danger)' }, { v: 60, label: '60% · atenção', color: 'var(--warn)' }],
  });
}
function bhEngTab() {
  const tb = bhId('bhEngTab'), L = bhLowRows();
  if (!L.length) return bhTbodyMsg(tb, 7, 'Ninguém abaixo de 70% do departamento neste filtro.');
  tb.replaceChildren();
  L.forEach(r => {
    const p = r.p, tr = bhPersonRow(p);
    tr.appendChild(bhEl('td', 'bh-small', p.depto));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(p.ativo_medio_min)));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(p.ref_depto_min)));
    const te = bhEl('td', 'bh-n'); te.appendChild(bhSevPill(bhEngSev(r.e), r.e + '%')); tr.appendChild(te);
    tr.appendChild(bhEl('td', 'bh-n', String(r.c.hollow)));
    tr.appendChild(bhEl('td', 'bh-n', String(r.c.absent)));
    tb.appendChild(tr);
  });
}
/* ---------------------------------------------------------------- Presente mas pouco ativo */
// % do expediente realmente ativo = tempo ativo / (fim - início) nos dias úteis trabalhados.
const BH_HOL_LIM = 27;   // abaixo disso o dia vira "presente mas pouco ativo"
function bhJornada(d) { const a = bhToMin(d.inicio); let b = bhToMin(d.fim); if (a == null || b == null) return 0; if (b < a) b += 1440; return b - a; }
function bhHolStats() {
  const days = {}, hol = {};
  bhRows('sinais').forEach(s => { if (s.tipo === 'hollow_day') (hol[s.user_id] = hol[s.user_id] || new Set()).add(s.dia); });
  bhRows('dias').forEach(d => {
    if (!bhTruthy(d.trabalhou) || bhTruthy(d.fim_de_semana_feriado)) return;
    const j = bhJornada(d); if (j < 60) return;
    (days[d.user_id] = days[d.user_id] || []).push({ dia: d.dia, j, a: bhNum(d.ativo_min), r: 100 * bhNum(d.ativo_min) / j });
  });
  return { days, hol };
}
function bhHolMedian(st) {
  const rs = Object.values(st.days).map(L => { const j = L.reduce((x, d) => x + d.j, 0), a = L.reduce((x, d) => x + d.a, 0); return j ? 100 * a / j : null; })
    .filter(v => v != null).sort((a, b) => a - b);
  return rs.length ? rs[Math.floor(rs.length / 2)] : 48;
}
function bhHolRows() {
  const st = bhHolStats();
  const L = bhRows('pessoas').filter(p => bhInDept(p.depto) && st.hol[p.user_id] && st.hol[p.user_id].size >= 2).map(p => {
    const ds = st.days[p.user_id] || [], hs = ds.filter(d => st.hol[p.user_id].has(d.dia));
    const j = ds.reduce((x, d) => x + d.j, 0), a = ds.reduce((x, d) => x + d.a, 0);
    const pior = hs.slice().sort((x, y) => x.r - y.r)[0] || null;
    return { p, n: st.hol[p.user_id].size, uteis: ds.length, r: j ? Math.round(100 * a / j) : 0,
      jm: ds.length ? j / ds.length : 0, am: ds.length ? a / ds.length : 0, pior };
  }).sort((x, y) => x.r - y.r || y.n - x.n);
  return { L, med: Math.round(bhHolMedian(st)) };
}
function bhHolChart() {
  const { L, med } = bhHolRows();
  const D = L.map(r => ({
    id: r.p.user_id, label: r.p.nome, v: r.r, color: r.r < BH_HOL_LIM ? 'var(--danger)' : (r.r < med - 10 ? 'var(--warn)' : 'var(--primary)'),
    text: r.r + '% · ' + r.n + (r.n === 1 ? ' dia' : ' dias'),
    tip: [r.p.nome, r.p.depto + (r.p.cargo ? ' · ' + r.p.cargo : ''),
      'Expediente médio ' + bhHm(r.jm) + ', ativo ' + bhHm(r.am) + ' (' + r.r + '% do expediente)',
      r.n + ' de ' + r.uteis + ' dias úteis com menos de ' + BH_HOL_LIM + '% ativo' + (r.pior ? ' · pior: ' + bhDia(r.pior.dia) + ' (' + Math.round(r.pior.r) + '%)' : '')],
  }));
  bhHbars(bhId('bhHolChart'), D, {
    aria: '% do expediente realmente ativo de quem teve 2+ dias presente mas pouco ativo', max: Math.max(70, med + 10),
    empty: 'Ninguém com 2 ou mais dias presente mas pouco ativo neste filtro.',
    refs: [{ v: BH_HOL_LIM, label: BH_HOL_LIM + '% · limite do sinal', color: 'var(--danger)' }, { v: med, label: med + '% · mediana da empresa', color: 'var(--text-muted)' }],
  });
  bhId('bhHolMed').textContent = med + '%';
}
function bhHolTab() {
  const tb = bhId('bhHolTab'), { L } = bhHolRows();
  if (!L.length) return bhTbodyMsg(tb, 7, 'Ninguém com 2 ou mais dias presente mas pouco ativo neste filtro.');
  tb.replaceChildren();
  L.forEach(r => {
    const p = r.p, tr = bhPersonRow(p);
    tr.appendChild(bhEl('td', 'bh-small', p.depto));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(r.jm)));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(r.am)));
    const tp = bhEl('td', 'bh-n'); tp.appendChild(bhSevPill(r.r < BH_HOL_LIM ? 'alta' : 'media', r.r + '%')); tr.appendChild(tp);
    tr.appendChild(bhEl('td', 'bh-n', r.n + ' de ' + r.uteis));
    tr.appendChild(bhEl('td', 'bh-small bh-nowrap', r.pior ? bhDia(r.pior.dia) + ' · ' + Math.round(r.pior.r) + '% de ' + bhHm(r.pior.j) : '—'));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- Muito tempo parado na mesma janela */
const BH_STA_RE = /^(\d+)h(\d+) seguidos em (.+?) às (\d\d:\d\d)/;
function bhStaRows() {
  const by = {};
  bhRows('sinais').forEach(s => {
    if (s.tipo !== 'long_static' || !bhInDept(s.depto)) return;
    const m = String(s.descricao || '').match(BH_STA_RE); if (!m) return;
    const min = (+m[1]) * 60 + (+m[2]);
    (by[s.user_id] = by[s.user_id] || []).push({ dia: s.dia, min, app: m[3], hora: m[4], ia: /ferramenta de IA/.test(s.descricao), tit: String(s.evidencia || '').replace(/^título:\s*/, '') });
  });
  const P = {}; bhRows('pessoas').forEach(p => { P[p.user_id] = p; });
  return Object.keys(by).filter(u => by[u].length >= 2 && P[u]).map(u => {
    const L = by[u], mx = L.slice().sort((a, b) => b.min - a.min)[0];
    const apps = {}; L.forEach(x => { apps[x.app] = (apps[x.app] || 0) + 1; });
    const top = Object.keys(apps).sort((a, b) => apps[b] - apps[a])[0];
    return { p: P[u], n: L.length, max: mx, tot: L.reduce((t, x) => t + x.min, 0), app: top, appN: apps[top], ia: L.filter(x => x.ia).length };
  }).sort((a, b) => b.max.min - a.max.min || b.n - a.n);
}
function bhStaChart() {
  const D = bhStaRows().map(r => ({
    id: r.p.user_id, label: r.p.nome, v: r.max.min,
    color: r.max.min >= 180 ? 'var(--danger)' : (r.max.min >= 120 ? 'var(--warn)' : 'var(--primary)'),
    text: bhHm(r.max.min) + ' · ' + r.n + 'x',
    tip: [r.p.nome, r.p.depto + (r.p.cargo ? ' · ' + r.p.cargo : ''),
      'Maior bloco: ' + bhHm(r.max.min) + ' em ' + r.max.app + ' (' + bhDia(r.max.dia) + ' às ' + r.max.hora + ')',
      r.n + ' blocos de 1h+ no período, ' + bhHm(r.tot) + ' no total' + (r.ia ? ' · ' + r.ia + ' em ferramenta de IA' : '')],
  }));
  bhHbars(bhId('bhStaChart'), D, {
    aria: 'Maior bloco seguido na mesma janela por pessoa', max: 240, empty: 'Ninguém com 2 ou mais blocos de 1h+ na mesma janela neste filtro.',
    refs: [{ v: 60, label: '1h · limite', color: 'var(--text-muted)' }, { v: 180, label: '3h', color: 'var(--danger)' }],
  });
}
function bhStaTab() {
  const tb = bhId('bhStaTab'), L = bhStaRows();
  if (!L.length) return bhTbodyMsg(tb, 6, 'Ninguém com 2 ou mais blocos de 1h+ na mesma janela neste filtro.');
  tb.replaceChildren();
  L.forEach(r => {
    const tr = bhPersonRow(r.p);
    tr.appendChild(bhEl('td', 'bh-small', r.p.depto));
    tr.appendChild(bhEl('td', 'bh-n', r.n + 'x'));
    const tm = bhEl('td', 'bh-n'); tm.appendChild(bhSevPill(r.max.min >= 180 ? 'alta' : (r.max.min >= 120 ? 'media' : 'baixa'), bhHm(r.max.min))); tr.appendChild(tm);
    tr.appendChild(bhEl('td', 'bh-n', bhHm(r.tot)));
    tr.appendChild(bhEl('td', 'bh-small', r.app + ' (' + r.appN + 'x)' + (r.ia === r.n ? ' · IA' : '')));
    tr.appendChild(bhEl('td', 'bh-small bh-nowrap', bhDia(r.max.dia) + ' às ' + r.max.hora));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- Atividade de madrugada */
// Noite = dia com 30+ min ativos entre 00h e 05h. "Contínua" = 3+ horas seguidas com 55+ min ativos
// (padrão de máquina mantida acordada / processo sem pausa — vale conferir).
function bhNigRows() {
  const by = {};
  bhRows('dias').forEach(d => {
    const m = bhNum(d.madrugada_min); if (m < 30) return;
    const h = String(d.por_hora || '').split(',').slice(0, 5).map(bhNum);
    let run = 0, best = 0; h.forEach(v => { run = v >= 55 ? run + 1 : 0; best = Math.max(best, run); });
    (by[d.user_id] = by[d.user_id] || []).push({ dia: d.dia, min: m, cont: best >= 3, off: bhTruthy(d.fim_de_semana_feriado) });
  });
  const P = {}; bhRows('pessoas').forEach(p => { P[p.user_id] = p; });
  const all = Object.keys(by).filter(u => P[u] && bhInDept(P[u].depto));
  bhNigRows.pontuais = all.filter(u => by[u].length < 2).length;
  return all.filter(u => by[u].length >= 2).map(u => {
    const L = by[u].sort((a, b) => a.dia < b.dia ? -1 : 1);
    const tot = L.reduce((t, x) => t + x.min, 0);
    return { p: P[u], n: L.length, tot, med: tot / L.length, cont: L.filter(x => x.cont).length, ult: L[L.length - 1], fds: L.filter(x => x.off).length };
  }).sort((a, b) => b.tot - a.tot);
}
function bhNigChart() {
  const D = bhNigRows().map(r => ({
    id: r.p.user_id, label: r.p.nome, v: r.tot,
    color: r.cont ? 'var(--danger)' : (r.n >= 3 ? 'var(--warn)' : 'var(--primary)'),
    text: bhHm(r.tot) + ' · ' + r.n + (r.n === 1 ? ' noite' : ' noites'),
    tip: [r.p.nome, r.p.depto + (r.p.cargo ? ' · ' + r.p.cargo : ''),
      r.n + ' noite(s) com 30+ min entre 00h e 05h, média de ' + bhHm(r.med) + ' por noite',
      r.cont ? r.cont + ' noite(s) com atividade contínua (3h+ seguidas sem pausa)' : 'Sem noites de atividade contínua',
      'Última: ' + bhDia(r.ult.dia)],
  }));
  bhHbars(bhId('bhNigChart'), D, { aria: 'Horas ativas entre 00h e 05h por pessoa no período', empty: 'Ninguém com 2 ou mais noites de atividade de madrugada neste filtro.' });
  const pn = bhNigRows.pontuais || 0;
  bhId('bhNigPont').textContent = pn ? pn + (pn === 1 ? ' pessoa teve' : ' pessoas tiveram') + ' só 1 noite no período e ficam de fora (aparecem na ficha e em Todos os sinais).' : '';
}
function bhNigTab() {
  const tb = bhId('bhNigTab'), L = bhNigRows();
  if (!L.length) return bhTbodyMsg(tb, 7, 'Ninguém com 2 ou mais noites de atividade de madrugada neste filtro.');
  tb.replaceChildren();
  L.forEach(r => {
    const tr = bhPersonRow(r.p);
    tr.appendChild(bhEl('td', 'bh-small', r.p.depto));
    tr.appendChild(bhEl('td', 'bh-n', r.n + (r.fds ? ' (' + r.fds + ' fds)' : '')));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(r.tot)));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(r.med)));
    const tc = bhEl('td', 'bh-n'); if (r.cont) tc.appendChild(bhSevPill('alta', String(r.cont))); else tc.textContent = '0'; tr.appendChild(tc);
    tr.appendChild(bhEl('td', 'bh-small bh-nowrap', bhDia(r.ult.dia) + ' · ' + bhHm(r.ult.min)));
    tb.appendChild(tr);
  });
}

function bhPersonRow(p) {
  const tr = bhEl('tr', 'bh-click' + (p.user_id === (bhCurrent() || {}).user_id ? ' bh-on' : ''));
  const c1 = bhEl('td'); c1.appendChild(bhEl('div', 'bh-name', p.nome)); c1.appendChild(bhEl('div', 'bh-small', p.cargo || p.user_id)); tr.appendChild(c1);
  tr.addEventListener('click', () => bhSelectPerson(p.user_id, true));
  return tr;
}

/* ---------------------------------------------------------------- Segurança */
const bhRiskSev = v => (v = bhNum(v)) >= 50 ? 'alta' : (v >= 20 ? 'media' : 'baixa');
function bhSecChart() {
  const g = {};
  bhRows('seguranca').filter(r => bhInDept(r.depto)).forEach(r => {
    const k = g[r.categoria] || (g[r.categoria] = { min: 0, pes: new Set(), dias: 0 });
    k.min += bhNum(r.minutos); k.pes.add(r.user_id); k.dias += bhNum(r.dias);
  });
  const L = Object.keys(g).map(k => {
    const o = g[k], n = o.pes.size;
    return { label: k, v: n, text: n + ' pess. · ' + bhHm(o.min), tip: [k, n + ' pessoa(s), ' + o.dias + ' dia(s)-pessoa', bhHm(o.min) + ' no total'] };
  }).sort((a, b) => b.v - a.v);
  bhHbars(bhId('bhSecChart'), L, { aria: 'Pessoas por categoria de risco de segurança', empty: 'Nenhum acesso de risco detectado neste filtro.' });
}
function bhSecTab() {
  const tb = bhId('bhSecTab');
  const L = bhRows('pessoas').filter(p => bhInDept(p.depto) && bhNum(p.risco) > 0).sort((a, b) => bhNum(b.risco) - bhNum(a.risco));
  if (!L.length) return bhTbodyMsg(tb, 5, 'Nenhuma pessoa com acesso de risco neste filtro.');
  tb.replaceChildren();
  L.forEach(p => {
    const tr = bhPersonRow(p);
    tr.appendChild(bhEl('td', 'bh-small', p.depto));
    const t = bhEl('td', 'bh-n'); t.appendChild(bhSevPill(bhRiskSev(p.risco), String(bhNum(p.risco)))); tr.appendChild(t);
    tr.appendChild(bhEl('td', 'bh-small', p.risco_categorias || '—'));
    tr.appendChild(bhEl('td', 'bh-small bh-ex', p.risco_exemplos || '—'));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- Redes sociais e WhatsApp */
function bhSocChart() {
  const L = bhRows('pessoas').filter(p => bhInDept(p.depto) && bhNum(p.social_min_dia) > 0)
    .sort((a, b) => bhNum(b.social_min_dia) - bhNum(a.social_min_dia)).slice(0, 15).map(p => {
      const v = bhNum(p.social_min_dia);
      return { id: p.user_id, label: p.nome, v, color: v >= 60 ? 'var(--danger)' : (v >= 30 ? 'var(--warn)' : 'var(--primary)'),
        text: v + ' min/dia · ' + bhPct(p.social_pct),
        tip: [p.nome, p.depto, v + ' min por dia útil em redes sociais', bhPct(p.social_pct) + ' do tempo ativo', p.social_sites ? 'Abas: ' + p.social_sites : ''] };
    });
  bhHbars(bhId('bhSocChart'), L, { aria: 'Minutos por dia útil em redes sociais', empty: 'Nenhum uso de redes sociais neste filtro.' });
}
function bhWaTab() {
  const tb = bhId('bhWaTab');
  const L = bhRows('pessoas').filter(p => bhInDept(p.depto) && bhNum(p.whatsapp_min_dia) > 0)
    .sort((a, b) => bhNum(b.whatsapp_min_dia) - bhNum(a.whatsapp_min_dia));
  if (!L.length) return bhTbodyMsg(tb, 5, 'Sem uso de WhatsApp registrado neste filtro.');
  tb.replaceChildren();
  L.forEach(p => {
    const tr = bhPersonRow(p);
    tr.appendChild(bhEl('td', 'bh-small', p.depto));
    tr.appendChild(bhEl('td', 'bh-n', String(bhNum(p.whatsapp_min_dia))));
    tr.appendChild(bhEl('td', 'bh-n', bhPct(p.whatsapp_pct)));
    tr.appendChild(bhEl('td', 'bh-small bh-ex', p.whatsapp_sites || '—'));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- Casa × escritório (sutil, neutro) */
const BH_WD = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex'];
function bhOffice() { return (BH.pack && BH.pack.meta && BH.pack.meta.dias_presenciais) || ['Seg', 'Qua', 'Sex']; }
function bhDowMap(p) { const m = {}; String(p.hib_por_dia || '').split(',').forEach(kv => { const [k, v] = kv.split(':'); if (k) m[k] = +v; }); return m; }
function bhDowBars(node, vals, opt) {
  node.replaceChildren();
  const ks = BH_WD.filter(k => vals[k] != null);
  if (!ks.length) return bhEmpty(node, opt.empty || 'Sem dias úteis suficientes.');
  const office = bhOffice();
  const W = Math.max(260, node.clientWidth || 420), H = opt.h || 150, top = 18, bot = 34, bw = Math.min(56, (W - 20) / 5 - 14);
  const mx = Math.max(...ks.map(k => vals[k])) || 1;
  const svg = bhSvg('svg', { width: '100%', height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opt.aria || '' }, node);
  BH_WD.forEach((k, i) => {
    const cx = (W / 5) * (i + 0.5), v = vals[k];
    const isOff = office.includes(k);
    bhSvg('text', { x: cx, y: H - 18, 'text-anchor': 'middle', class: 'bh-lbl-strong' }, svg).textContent = k;
    bhSvg('text', { x: cx, y: H - 4, 'text-anchor': 'middle', class: 'bh-lbl' }, svg).textContent = isOff ? 'escritório' : 'casa';
    if (v == null) return;
    const h = (H - top - bot) * v / mx;
    const r = bhSvg('rect', { x: cx - bw / 2, y: H - bot - h, width: bw, height: Math.max(1, h), rx: 3,
      fill: isOff ? 'var(--primary)' : 'var(--violet, #a78bfa)', 'fill-opacity': 0.85 }, svg);
    bhSvg('text', { x: cx, y: H - bot - h - 5, 'text-anchor': 'middle', class: 'bh-lbl' }, svg).textContent = bhHm(v);
    bhHover(r, [k + ' · ' + (isOff ? 'presencial' : 'em casa'), bhHm(v) + ' ativo em média' + (opt.n && opt.n[k] ? ' (' + opt.n[k] + ' dias-pessoa)' : '')]);
  });
}
function bhHibChart() {
  const sum = {}, n = {};
  bhRows('pessoas').filter(p => bhInDept(p.depto)).forEach(p => {
    const m = bhDowMap(p);
    Object.keys(m).forEach(k => { sum[k] = (sum[k] || 0) + m[k]; n[k] = (n[k] || 0) + 1; });
  });
  const avg = {}; Object.keys(sum).forEach(k => { avg[k] = sum[k] / n[k]; });
  bhDowBars(bhId('bhHibChart'), avg, { aria: 'Tempo ativo médio da equipe por dia da semana', n, h: 190 });
}
function bhHibRows() {
  return bhRows('pessoas').filter(p => bhInDept(p.depto) && p.hib_dif_pct != null && bhNum(p.hib_dias_escritorio) >= 3 && bhNum(p.hib_dias_casa) >= 2
    && Math.abs(bhNum(p.hib_dif_pct)) >= 20 && Math.abs(bhNum(p.hib_casa_min) - bhNum(p.hib_escritorio_min)) >= 45)
    .sort((a, b) => bhNum(a.hib_dif_pct) - bhNum(b.hib_dif_pct));
}
function bhHibTab() {
  const tb = bhId('bhHibTab'), L = bhHibRows();
  bhId('bhHibCount').textContent = L.length ? L.length + ' pessoa(s) com diferença de 20% ou mais entre os dois tipos de dia' : '';
  if (!L.length) return bhTbodyMsg(tb, 6, 'Ritmo parecido em casa e no escritório neste filtro.');
  tb.replaceChildren();
  L.forEach(p => {
    const tr = bhPersonRow(p), d = bhNum(p.hib_dif_pct);
    tr.appendChild(bhEl('td', 'bh-small', p.depto));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(p.hib_escritorio_min) + ' · ' + p.hib_dias_escritorio + 'd'));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(p.hib_casa_min) + ' · ' + p.hib_dias_casa + 'd'));
    const td = bhEl('td', 'bh-n'); td.appendChild(bhEl('span', 'pill bh-sev-baixa', (d > 0 ? '+' : '') + d + '% em casa')); tr.appendChild(td);
    tr.appendChild(bhEl('td', 'bh-small', p.hib_menos_ativo || '—'));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- Sinais por tipo */
function bhTipos() {
  const node = bhId('bhTipos'); node.replaceChildren();
  const g = {};
  bhRows('sinais').filter(s => bhInDept(s.depto)).forEach(s => {
    const k = g[s.sinal] || (g[s.sinal] = { sinal: s.sinal, tipo: s.tipo, alta: 0, media: 0, baixa: 0, pes: new Set() });
    k[s.severidade]++; k.pes.add(s.user_id);
  });
  const L = Object.values(g).map(o => ({ ...o, total: o.alta + o.media + o.baixa, pessoas: o.pes.size })).sort((a, b) => b.total - a.total);
  if (!L.length) return bhEmpty(node, 'Nenhum sinal neste filtro.');
  const W = Math.max(300, node.clientWidth || 560), rowH = 30, H = L.length * rowH + 6;
  let lw = 0, tw = 0;
  L.forEach(d => { lw = Math.max(lw, bhTextW(d.sinal, 12, 500)); tw = Math.max(tw, bhTextW(d.total + ' · ' + d.pessoas + ' pess.', 12)); });
  lw = Math.min(lw + 14, W * 0.48);
  const mx = Math.max(...L.map(d => d.total)) || 1, x1 = Math.max(lw + 40, W - tw - 12);
  const X = v => (x1 - lw) * v / mx;
  const svg = bhSvg('svg', { width: '100%', height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Sinais por tipo e severidade' }, node);
  const col = { alta: 'var(--danger)', media: 'var(--warn)', baixa: 'var(--text-muted)' };
  L.forEach((d, i) => {
    const y = i * rowH + 4, g2 = bhSvg('g', { class: 'bh-click' }, svg);
    bhSvg('rect', { x: 0, y: y - 2, width: W, height: rowH - 2, fill: 'transparent' }, g2);
    bhSvg('text', { x: lw - 10, y: y + 15, 'text-anchor': 'end', class: 'bh-lbl-strong' }, g2).textContent = bhFit(d.sinal, lw - 14, 12, 500);
    let x = lw;
    ['alta', 'media', 'baixa'].forEach(s => {
      if (!d[s]) return; const w = X(d[s]);
      bhSvg('rect', { x, y: y + 4, width: Math.max(1, w), height: rowH - 12, rx: 2, fill: col[s] }, g2); x += w;
    });
    bhSvg('text', { x: x + 7, y: y + 15, class: 'bh-lbl' }, g2).textContent = d.total + ' · ' + d.pessoas + ' pess.';
    bhHover(g2, [d.sinal, d.total + ' sinais em ' + d.pessoas + ' pessoas', 'Alta ' + d.alta + ' · Média ' + d.media + ' · Baixa ' + d.baixa]);
    g2.addEventListener('click', () => { BH.sigType = d.tipo; bhSigTipoSelect(); bhSinaisTab(); bhId('bhTodos').scrollIntoView({ behavior: 'smooth', block: 'start' }); });
  });
}

/* ---------------------------------------------------------------- mapa de calor da equipe */
function bhHeat() {
  const node = bhId('bhHeat'); node.replaceChildren();
  const dep = {}; bhRows('pessoas').forEach(p => { dep[p.user_id] = p.depto; });
  const order = [1, 2, 3, 4, 5, 6, 0], sum = {}, cnt = {};
  order.forEach(w => { sum[w] = new Array(24).fill(0); cnt[w] = 0; });
  bhRows('dias').forEach(d => {
    if (!bhTruthy(d.trabalhou) || !bhInDept(dep[d.user_id])) return;
    const w = new Date(d.dia + 'T12:00:00').getDay();
    const a = String(BH.heatMode === 'nw' ? d.nao_trabalho_por_hora : d.por_hora).split(',');
    for (let h = 0; h < 24; h++) sum[w][h] += bhNum(a[h]);
    cnt[w]++;
  });
  let mx = 0; const grid = [];
  order.forEach((w, r) => { for (let h = 0; h < 24; h++) { const v = cnt[w] ? sum[w][h] / cnt[w] : 0; mx = Math.max(mx, v); grid.push({ r, w, h, v, n: cnt[w] }); } });
  const W = Math.max(320, node.clientWidth || 560), left = 36, top = 18, cw = (W - left) / 24, ch = Math.max(18, Math.min(30, cw * 1.1)), H = top + 7 * ch + 4;
  const svg = bhSvg('svg', { width: '100%', height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Minutos por pessoa por hora e dia da semana' }, node);
  const color = BH.heatMode === 'nw' ? 'var(--warn)' : 'var(--primary)';
  for (let h = 0; h < 24; h += 3) bhSvg('text', { x: left + h * cw + cw / 2, y: 11, 'text-anchor': 'middle', class: 'bh-lbl' }, svg).textContent = h + 'h';
  order.forEach((w, r) => { bhSvg('text', { x: 0, y: top + r * ch + ch / 2 + 4, class: 'bh-lbl' }, svg).textContent = BH_DOW[w]; });
  grid.forEach(c => {
    const rc = bhSvg('rect', { x: left + c.h * cw + 1, y: top + c.r * ch + 1, width: Math.max(1, cw - 2), height: ch - 2, rx: 2,
      fill: c.v > 0 ? color : 'var(--border-soft)', 'fill-opacity': c.v > 0 ? (0.12 + 0.88 * (mx ? c.v / mx : 0)) : 0.5 }, svg);
    bhHover(rc, [BH_DOW[c.w] + ' · ' + c.h + 'h–' + (c.h + 1) + 'h', Math.round(c.v) + ' min ' + (BH.heatMode === 'nw' ? 'de não-trabalho' : 'ativos') + ' por pessoa', c.n + ' dias-pessoa considerados']);
  });
}
function bhHeatMode(m) {
  BH.heatMode = m;
  document.querySelectorAll('.bh-heat-btn').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  bhHeat();
}

/* ---------------------------------------------------------------- início × fim */
function bhScatter() {
  const node = bhId('bhScatter'); node.replaceChildren();
  const ps = bhRows('pessoas').filter(p => bhInDept(p.depto) && p.inicio_mediano && p.fim_mediano).map(p => {
    const a = bhToMin(p.inicio_mediano); let b = bhToMin(p.fim_mediano); if (b < a) b += 1440; return { p, x: a, y: b };
  });
  if (!ps.length) return bhEmpty(node, 'Sem pessoas neste filtro.');
  const W = Math.max(320, node.clientWidth || 900), H = 330, m = { l: 46, r: 14, t: 10, b: 30 };
  const xs = ps.map(d => d.x), ys = ps.map(d => d.y);
  const xd = [Math.floor(Math.min(...xs) / 60) * 60, Math.ceil(Math.max(...xs) / 60) * 60 + 1];
  const yd = [Math.floor(Math.min(...ys) / 60) * 60, Math.ceil(Math.max(...ys) / 60) * 60 + 1];
  const X = v => m.l + (W - m.l - m.r) * (v - xd[0]) / (xd[1] - xd[0]);
  const Y = v => (H - m.b) - (H - m.b - m.t) * (v - yd[0]) / (yd[1] - yd[0]);
  const maxP = Math.max(1, ...ps.map(d => bhNum(d.p.pontos)));
  const R = v => 3 + 10 * Math.sqrt(v / maxP);
  const svg = bhSvg('svg', { width: '100%', height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Início contra fim do expediente por pessoa' }, node);
  const hh = v => String(Math.floor(v / 60) % 24).padStart(2, '0') + 'h';
  const stepX = (xd[1] - xd[0]) / 60 > 10 ? 120 : 60;
  for (let v = xd[0]; v <= xd[1]; v += stepX) bhSvg('text', { x: X(v), y: H - 10, 'text-anchor': 'middle', class: 'bh-lbl' }, svg).textContent = hh(v);
  const stepY = (yd[1] - yd[0]) / 60 > 8 ? 120 : 60;
  for (let v = yd[0]; v <= yd[1]; v += stepY) {
    bhSvg('line', { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: 'var(--border-soft)' }, svg);
    bhSvg('text', { x: m.l - 6, y: Y(v) + 4, 'text-anchor': 'end', class: 'bh-lbl' }, svg).textContent = hh(v);
  }
  bhSvg('text', { x: W - m.r, y: H - 22, 'text-anchor': 'end', class: 'bh-lbl' }, svg).textContent = 'início →';
  bhSvg('text', { x: m.l + 4, y: m.t + 10, class: 'bh-lbl' }, svg).textContent = '↑ fim';
  const cur = (bhCurrent() || {}).user_id;
  ps.sort((a, b) => bhNum(b.p.pontos) - bhNum(a.p.pontos)).forEach(d => {
    const on = d.p.user_id === cur;
    const c = bhSvg('circle', { cx: X(d.x), cy: Y(d.y), r: R(bhNum(d.p.pontos)), class: 'bh-click',
      fill: bhNum(d.p.sinais_alta) > 0 ? 'var(--danger)' : 'var(--primary)', 'fill-opacity': 0.55,
      stroke: on ? 'var(--text)' : 'var(--bg)', 'stroke-width': on ? 2 : 1 }, svg);
    bhHover(c, [d.p.nome, d.p.depto + (d.p.cargo ? ' · ' + d.p.cargo : ''), 'Início ' + d.p.inicio_mediano + ' · fim ' + d.p.fim_mediano + ' · ' + bhHm(d.p.ativo_medio_min) + ' ativo/dia', bhNum(d.p.pontos) + ' pontos (' + bhNum(d.p.sinais_alta) + ' alta)']);
    c.addEventListener('click', () => bhSelectPerson(d.p.user_id, true));
  });
}

/* ---------------------------------------------------------------- ranking */
function bhRankRows() {
  return bhRows('pessoas').filter(p => {
    if (!bhInDept(p.depto)) return false;
    if (BH.onlyHigh && !bhNum(p.sinais_alta)) return false;
    if (BH.q && (p.nome + ' ' + p.cargo + ' ' + p.depto + ' ' + p.user_id).toLowerCase().indexOf(BH.q) < 0) return false;
    return true;
  }).sort((a, b) => bhNum(b.pontos) - bhNum(a.pontos) || bhNum(b.sinais_alta) - bhNum(a.sinais_alta));
}
function bhRanking() {
  const L = bhRankRows(), tb = bhId('bhRanking');
  bhId('bhRankCount').textContent = L.length + ' de ' + bhRows('pessoas').length + ' colaboradores';
  if (!L.length) return bhTbodyMsg(tb, 11, 'Ninguém neste filtro.');
  tb.replaceChildren();
  L.forEach(p => {
    const tr = bhPersonRow(p);
    tr.appendChild(bhEl('td', 'bh-small', p.depto));
    tr.appendChild(bhEl('td', 'bh-n', String(bhNum(p.pontos))));
    ['alta', 'media', 'baixa'].forEach(s => {
      const td = bhEl('td', 'bh-n'), v = bhNum(p['sinais_' + s]);
      if (v) td.appendChild(bhSevPill(s, String(v))); else td.textContent = '0';
      tr.appendChild(td);
    });
    tr.appendChild(bhEl('td', 'bh-small', p.principais_sinais || '—'));
    tr.appendChild(bhEl('td', 'bh-n', p.inicio_mediano || '—'));
    tr.appendChild(bhEl('td', 'bh-n', p.fim_mediano || '—'));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(p.ativo_medio_min)));
    tr.appendChild(bhEl('td', 'bh-n', bhPct(p.nao_trabalho_pct)));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- ficha da pessoa */
function bhCurrent() {
  const ps = bhRows('pessoas'); if (!ps.length) return null;
  if (BH.sel) { const f = ps.find(p => p.user_id === BH.sel); if (f) return f; }
  return ps.slice().sort((a, b) => bhNum(b.pontos) - bhNum(a.pontos))[0];
}
function bhSelectPerson(id, scroll) {
  BH.sel = id; bhHideTip();
  bhRenderPerson();
  // destaca a linha/ponto selecionado nas tabelas e no gráfico
  bhEngTab(); bhSecTab(); bhWaTab(); bhRanking(); bhScatter();
  if (scroll) bhId('bhPessoa').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
function bhRenderPerson() {
  const p = bhCurrent(); if (!p) return;
  bhId('bhPNome').textContent = p.nome;
  const parts = [p.cargo, p.depto, p.gestor ? 'Gestor: ' + p.gestor : '', p.user_id].filter(Boolean);
  if (p.inicio_mediano) parts.push('Padrão ' + p.inicio_mediano + '–' + p.fim_mediano + ', ' + bhHm(p.ativo_medio_min) + ' ativo/dia');
  bhId('bhPInfo').textContent = parts.join(' · ');
  const bd = bhId('bhPBadges'); bd.replaceChildren();
  ['alta', 'media', 'baixa'].forEach(s => bd.appendChild(bhSevPill(s, BH_SEV_LABEL[s] + ': ' + bhNum(p['sinais_' + s]))));
  if (bhTruthy(p.ferias)) bd.appendChild(bhEl('span', 'pill', 'Marcado de férias'));
  const prof = bhEl('button', 'ghost bh-prof', 'Abrir perfil ↗');
  prof.addEventListener('click', () => { if (typeof openProfileModal === 'function') openProfileModal(p.user_id, p.nome); });
  bd.appendChild(prof);
  // métricas extras
  const ex = bhId('bhPExtra'); ex.replaceChildren();
  const item = (lbl, val, sev) => { const d = bhEl('div', 'bh-mini'); d.appendChild(bhEl('span', 'bh-mini-l', lbl)); d.appendChild(bhEl('span', 'bh-mini-v' + (sev ? ' bh-mini-' + sev : ''), val)); ex.appendChild(d); };
  const e = (p.engajamento == null || p.engajamento === '') ? null : bhNum(p.engajamento);
  item('Tempo logado vs depto', e == null ? '—' : e + '%', e == null ? '' : (e < 45 ? 'alta' : (e < 60 ? 'media' : '')));
  item('Risco de segurança', bhNum(p.risco) + '/100', bhNum(p.risco) >= 50 ? 'alta' : (bhNum(p.risco) >= 20 ? 'media' : ''));
  item('Redes sociais', bhNum(p.social_min_dia) + ' min/dia', bhNum(p.social_min_dia) >= 60 ? 'alta' : (bhNum(p.social_min_dia) >= 30 ? 'media' : ''));
  item('WhatsApp', bhNum(p.whatsapp_min_dia) + ' min/dia', '');
  [['Redes sociais (abas): ', p.social_sites], ['WhatsApp (onde): ', p.whatsapp_sites], ['Vídeo (abas): ', p.video_sites], ['Acessos de risco: ', p.risco_exemplos]]
    .forEach(([l, v]) => { if (v) ex.appendChild(bhEl('div', 'bh-small bh-mini-ex', l + v)); });
  bhId('bhPResumo').textContent = p.resumo || '—';
  const dm = bhDowMap(p);
  bhDowBars(bhId('bhPDow'), dm, { aria: 'Tempo ativo médio por dia da semana de ' + p.nome, empty: 'Sem dias úteis suficientes para comparar.' });
  bhId('bhPDowTxt').textContent = (p.hib_dif_pct == null) ? '' :
    'Média de ' + bhHm(p.hib_escritorio_min) + ' nos dias presenciais (' + p.hib_dias_escritorio + ') e ' + bhHm(p.hib_casa_min) +
    ' em casa (' + p.hib_dias_casa + '). Dia menos ativo: ' + (p.hib_menos_ativo || '—') + '.';
  bhPHeat(p); bhPSinais(p); bhPDias(p);
}
function bhPHeat(p) {
  const node = bhId('bhPHeat'); node.replaceChildren();
  const ds = bhRows('dias').filter(d => d.user_id === p.user_id).sort((a, b) => a.dia < b.dia ? -1 : 1);
  if (!ds.length) return bhEmpty(node, 'Sem dias registrados.');
  const W = Math.max(320, node.clientWidth || 900), left = 74, top = 18, cw = (W - left) / 24, ch = Math.max(14, Math.min(22, cw)), H = top + ds.length * ch + 4;
  const svg = bhSvg('svg', { width: '100%', height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Atividade por hora em cada dia para ' + p.nome }, node);
  for (let h = 0; h < 24; h += 3) bhSvg('text', { x: left + h * cw + cw / 2, y: 11, 'text-anchor': 'middle', class: 'bh-lbl' }, svg).textContent = h + 'h';
  ds.forEach((d, r) => {
    bhSvg('text', { x: 0, y: top + r * ch + ch / 2 + 4, class: 'bh-lbl' }, svg).textContent = bhDia(d.dia) + (bhTruthy(d.fim_de_semana_feriado) ? ' ◦' : '');
    const a = String(d.por_hora || '').split(','), n = String(d.nao_trabalho_por_hora || '').split(',');
    for (let h = 0; h < 24; h++) {
      const v = bhNum(a[h]), nv = bhNum(n[h]), isNw = v > 0 && nv / v > 0.5;
      const rc = bhSvg('rect', { x: left + h * cw + 1, y: top + r * ch + 1, width: Math.max(1, cw - 2), height: ch - 2, rx: 2,
        fill: v > 0 ? (isNw ? 'var(--warn)' : 'var(--primary)') : 'var(--border-soft)', 'fill-opacity': v > 0 ? (0.15 + 0.85 * Math.min(1, v / 60)) : 0.45 }, svg);
      bhHover(rc, [bhDia(d.dia) + ' · ' + h + 'h', v + ' min ativos' + (nv ? ', ' + nv + ' não-trabalho' : ''), d.top_apps ? 'Apps do dia: ' + d.top_apps : '']);
    }
  });
}
function bhPSinais(p) {
  const tb = bhId('bhPSinais');
  const L = bhRows('sinais').filter(s => s.user_id === p.user_id)
    .sort((a, b) => (a.dia < b.dia ? -1 : a.dia > b.dia ? 1 : 0) || BH_SEV_ORD[a.severidade] - BH_SEV_ORD[b.severidade]);
  if (!L.length) return bhTbodyMsg(tb, 5, 'Nenhum sinal no período — comportamento estável.');
  tb.replaceChildren();
  L.forEach(s => {
    const tr = bhEl('tr');
    tr.appendChild(bhEl('td', 'bh-nowrap', bhDia(s.dia)));
    const td = bhEl('td'); td.appendChild(bhSevPill(s.severidade)); tr.appendChild(td);
    tr.appendChild(bhEl('td', 'bh-nowrap', s.sinal)); tr.appendChild(bhEl('td', '', s.descricao)); tr.appendChild(bhEl('td', 'bh-small', s.evidencia || ''));
    tb.appendChild(tr);
  });
}
function bhPDias(p) {
  const tb = bhId('bhPDias');
  const L = bhRows('dias').filter(d => d.user_id === p.user_id && bhTruthy(d.trabalhou)).sort((a, b) => a.dia < b.dia ? 1 : -1);
  if (!L.length) return bhTbodyMsg(tb, 10, 'Sem dias trabalhados.');
  tb.replaceChildren();
  L.forEach(d => {
    const tr = bhEl('tr');
    [bhDia(d.dia) + (bhTruthy(d.fim_de_semana_feriado) ? ' ◦' : ''), d.primeira_atividade || '—', d.inicio || '—', d.inicio_efetivo || '—', d.fim || '—']
      .forEach(v => tr.appendChild(bhEl('td', 'bh-nowrap', v)));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(d.ativo_min)));
    tr.appendChild(bhEl('td', 'bh-n', d.maior_pausa_min == null ? '—' : bhHm(d.maior_pausa_min)));
    tr.appendChild(bhEl('td', 'bh-n', bhHm(d.nao_trabalho_min)));
    tr.appendChild(bhEl('td', 'bh-small', d.maior_bloco || '—'));
    tr.appendChild(bhEl('td', 'bh-small', d.top_apps || '—'));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- todos os sinais */
function bhSigRows() {
  return bhRows('sinais').filter(s => bhInDept(s.depto) && (!BH.sigType || s.tipo === BH.sigType) && (!BH.sigSev || s.severidade === BH.sigSev))
    .sort((a, b) => BH_SEV_ORD[a.severidade] - BH_SEV_ORD[b.severidade] || bhNum(b.score) - bhNum(a.score));
}
function bhSigTipoSelect() {
  const sel = bhId('bhSigTipo'), c = {}, lab = {};
  bhRows('sinais').forEach(s => { c[s.tipo] = (c[s.tipo] || 0) + 1; lab[s.tipo] = s.sinal; });
  sel.replaceChildren(new Option('Todos os tipos de sinal', ''));
  Object.keys(c).sort((a, b) => c[b] - c[a]).forEach(t => sel.appendChild(new Option(lab[t] + ' (' + c[t] + ')', t)));
  sel.value = BH.sigType;
}
function bhSinaisTab() {
  const all = bhSigRows(), L = all.slice(0, 300), tb = bhId('bhSinais');
  bhId('bhSigCount').textContent = all.length + ' sinais' + (all.length > 300 ? ' (mostrando 300)' : '');
  if (!L.length) return bhTbodyMsg(tb, 6, 'Nenhum sinal neste filtro.');
  tb.replaceChildren();
  L.forEach(s => {
    const tr = bhEl('tr', 'bh-click');
    const td = bhEl('td'); td.appendChild(bhSevPill(s.severidade)); tr.appendChild(td);
    tr.appendChild(bhEl('td', 'bh-nowrap', s.nome || s.user_id)); tr.appendChild(bhEl('td', 'bh-small', s.depto || ''));
    tr.appendChild(bhEl('td', 'bh-nowrap', bhDia(s.dia))); tr.appendChild(bhEl('td', 'bh-nowrap', s.sinal)); tr.appendChild(bhEl('td', '', s.descricao));
    tr.addEventListener('click', () => bhSelectPerson(s.user_id, true));
    tb.appendChild(tr);
  });
}

/* ---------------------------------------------------------------- ligações dos controles */
(function bhWire() {
  const sec = bhId('tab-behavior'); if (!sec) return;
  document.querySelectorAll('.bh-days button').forEach(b => b.addEventListener('click', () => bhSetDays(+b.dataset.days)));
  bhId('bhDept').addEventListener('change', e => { BH.dept = e.target.value; bhRenderFiltered(); });
  bhId('bhRebuild').addEventListener('click', bhRebuild);
  bhId('bhSearch').addEventListener('input', e => { BH.q = e.target.value.trim().toLowerCase(); bhRanking(); });
  bhId('bhOnlyHigh').addEventListener('change', e => { BH.onlyHigh = e.target.checked; bhRanking(); });
  bhId('bhSigTipo').addEventListener('change', e => { BH.sigType = e.target.value; bhSinaisTab(); });
  bhId('bhSigSev').addEventListener('change', e => { BH.sigSev = e.target.value; bhSinaisTab(); });
  document.querySelectorAll('.bh-heat-btn').forEach(b => b.addEventListener('click', () => bhHeatMode(b.dataset.mode)));
  // redesenha os SVG ao mudar a largura (sem baixar nada de novo)
  let rt = null;
  addEventListener('resize', () => {
    if (sec.classList.contains('hide') || !BH.pack) return;
    clearTimeout(rt); rt = setTimeout(() => { bhRenderFiltered(); bhRenderPerson(); }, 150);
  });
})();
