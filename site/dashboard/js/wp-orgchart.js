// =====================================================================
// wp-orgchart.js — Análises → Organograma
// ---------------------------------------------------------------------
// Árvore hierárquica interativa a partir de GET /v1/org-chart: roster
// COMPLETO da fonte de RH (Convenia), não só quem tem agente WorkPulse
// instalado. Cada pessoa vem com { id, user_id, has_agent, display_name,
// email, department, manager, cargo, team, photo_url, on_vacation }.
//
// Montagem da árvore: casa `manager` (texto) de cada pessoa com o
// `display_name` de outra pessoa da mesma lista (ambos vindos da mesma
// fonte — Convenia — então o match tende a ser exato; normalizamos acento/
// caixa por segurança).
//
// Regra de raiz (2026-07-24, ajustada depois do 1º teste com dados reais):
// quem tem gestor vinculado aparece como filho dele. Quem NÃO tem gestor
// (vazio ou sem correspondência) só aparece no organograma se tiver pelo
// menos 1 liderado — vira uma "raiz". Quem não tem gestor E não lidera
// ninguém fica de fora (sem nenhuma relação, não ajuda o organograma).
//
// Entre as raízes, UMA é a "raiz primária" (nível 0 de verdade — ex.: o
// CEO): escolhida pelo cargo (CEO/Presidente/Fundador/…) ou, na falta
// disso, por ser quem tem a maior estrutura abaixo. As DEMAIS raízes
// (gente que devia ter gestor mas o RH esqueceu de cadastrar) ficam
// ocultas por padrão (flag "Ocultar sem gestor", ligada por padrão) pra
// não aparecerem como caixinhas soltas do lado do CEO — dá pra desligar o
// flag pra auditar quem está sem gestor cadastrado.
//
// Layout: árvore clássica <ul>/<li> em fileira ÚNICA sempre — ao expandir,
// os cards crescem pro lado (empurrando os irmãos), com rolagem horizontal
// e clicar-e-arrastar (pan) no canvas. Duas tentativas de "quebrar" times
// grandes em mais de 1 linha foram revertidas por causarem bugs reais:
// (1) modo de grade (CSS grid) comprimia/sobrepunha os cards; (2) fileiras
// empilhadas (<ul>s múltiplos) descentralizavam/quebravam a linha de
// conexão ao expandir um card no MEIO de uma fileira. Fileira única é o
// que se mostrou confiável — pan + zoom + scroll cobrem o caso de muitos
// liderados.
//
// Ícone de agente: bolinha verde/vermelha no canto da foto — verde = tem
// agente WorkPulse instalado; vermelho = só cadastro na Convenia. Clique
// na foto/nome: se tem agente, abre a Ficha completa (modal já existente);
// se não tem, abre um resumo local (sem chamada de API).
// =====================================================================

const OC_PALETTE = ['var(--primary)', 'var(--accent)', 'var(--violet)', 'var(--teal)', 'var(--pink)', 'var(--warn)'];
const OC_TOP_TITLE_RE = /\b(ceo|presidente|fundador|founder|owner|diretor presidente|diretora presidente|diretor geral|diretora geral)\b/;

const _oc = {
  people:      [],
  byId:        {},
  childrenOf:  {},
  parentOf:    {},
  roots:       [],
  primaryRoot: null,
  shown:       new Set(),
  deptColors:  {},
  expanded:    new Set(),
  matches:     new Set(),
  levels:      2,
  zoom:        1,
  hideOrphans: true,
  loaded:      false,
};

/* ---------- Normalização p/ casar manager (texto) com display_name ---------- */
function _ocNorm(s) {
  return (s || '')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/\s+/g, ' ').trim();
}

function _ocSubtreeSize(id, childrenOf) {
  let n = 1;
  (childrenOf[id] || []).forEach(c => { n += _ocSubtreeSize(c, childrenOf); });
  return n;
}

/* Escolhe a raiz "de verdade" (nível 0) entre as raízes encontradas:
   1) alguém com cargo de topo (CEO/Presidente/Fundador/…); 2) na falta
   disso, quem tem a maior estrutura (mais gente abaixo, direta+indireta). */
function _ocPickPrimaryRoot(roots, byId, childrenOf) {
  if (!roots.length) return null;
  const byTitle = roots.find(id => OC_TOP_TITLE_RE.test(_ocNorm(byId[id].cargo || '')));
  if (byTitle) return byTitle;
  let best = roots[0], bestSize = -1;
  roots.forEach(id => {
    const sz = _ocSubtreeSize(id, childrenOf);
    if (sz > bestSize) { best = id; bestSize = sz; }
  });
  return best;
}

function _ocBuildTree(people) {
  const byId = {};
  const byNameNorm = {};
  people.forEach(p => {
    byId[p.id] = p;
    const key = _ocNorm(p.display_name || '');
    if (key && !(key in byNameNorm)) byNameNorm[key] = p.id;
  });

  const childrenOf = {};
  const parentOf = {};
  people.forEach(p => {
    const mgrKey = _ocNorm(p.manager || '');
    const parentId = mgrKey ? byNameNorm[mgrKey] : null;
    if (parentId && parentId !== p.id) {
      parentOf[p.id] = parentId;
      (childrenOf[parentId] = childrenOf[parentId] || []).push(p.id);
    } else {
      parentOf[p.id] = null;
    }
  });

  const byName = (a, b) => (byId[a].display_name || '').localeCompare(byId[b].display_name || '', 'pt-BR');
  Object.values(childrenOf).forEach(arr => arr.sort(byName));

  const roots = people
    .filter(p => !parentOf[p.id] && (childrenOf[p.id] || []).length > 0)
    .map(p => p.id)
    .sort(byName);

  const shown = new Set();
  function walk(id) {
    if (shown.has(id)) return; // guarda contra ciclo
    shown.add(id);
    (childrenOf[id] || []).forEach(walk);
  }
  roots.forEach(walk);

  const primaryRoot = _ocPickPrimaryRoot(roots, byId, childrenOf);

  return { byId, childrenOf, parentOf, roots, shown, primaryRoot };
}

function _ocDefaultExpand(levels) {
  const exp = new Set();
  function walk(id, depth) {
    if (depth < levels - 1) {
      exp.add(id);
      (_oc.childrenOf[id] || []).forEach(c => walk(c, depth + 1));
    }
  }
  _ocVisibleRoots().forEach(r => walk(r, 0));
  return exp;
}

function _ocBuildDeptColors(people) {
  const depts = [...new Set(people.map(p => (p.department || '').trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, 'pt-BR'));
  const map = {};
  depts.forEach((d, i) => { map[d] = OC_PALETTE[i % OC_PALETTE.length]; });
  return map;
}

/* Raízes efetivamente exibidas: só a primária quando "ocultar sem gestor"
   está ligado; todas quando está desligado (modo auditoria). */
function _ocVisibleRoots() {
  if (!_oc.roots.length) return [];
  if (_oc.hideOrphans) return _oc.primaryRoot ? [_oc.primaryRoot] : [];
  return _oc.roots;
}

/* ---------- Render ---------- */
function _ocPhotoHtml(p, large) {
  const name = p.display_name || '—';
  if (large) {
    if (p.photo_url) {
      return `<img class="wp-modal-photo" src="${escHtml(p.photo_url)}" alt="${escHtml(name)}"
        onerror="this.outerHTML='&lt;div class=&quot;wp-modal-photo lg&quot;&gt;${_wpInitial(name)}&lt;/div&gt;'">`;
    }
    return `<div class="wp-modal-photo lg">${_wpInitial(name)}</div>`;
  }
  if (p.photo_url) {
    return `<img class="oc-photo" src="${escHtml(p.photo_url)}" alt="${escHtml(name)}" loading="lazy"
      onerror="this.outerHTML='&lt;div class=&quot;oc-photo-fallback&quot;&gt;${_wpInitial(name)}&lt;/div&gt;'">`;
  }
  return `<div class="oc-photo-fallback">${_wpInitial(name)}</div>`;
}

function _ocPhotoBlock(p) {
  const hasAgent = !!p.has_agent;
  const title = hasAgent ? 'Com agente WorkPulse instalado' : 'Sem agente instalado — só cadastro na Convenia';
  return `<span class="oc-photo-wrap">
    ${_ocPhotoHtml(p, false)}
    <span class="oc-agent-badge ${hasAgent ? 'yes' : 'no'}" title="${title}"></span>
  </span>`;
}

function _ocCardHtml(id, isRoot) {
  const p = _oc.byId[id];
  const name = p.display_name || '—';
  const dept = (p.department || '').trim();
  const color = dept ? (_oc.deptColors[dept] || null) : null;
  const kids = _oc.childrenOf[id] || [];
  const isExp = _oc.expanded.has(id);
  const isMatch = _oc.matches.has(id);
  const isPrimary = id === _oc.primaryRoot;
  const isOrphanRoot = isRoot && !isPrimary;
  const cls = ['oc-card'];
  if (isRoot) cls.push('root');
  if (isMatch) cls.push('match');
  const deptStyle = color
    ? `background:color-mix(in srgb, ${color} 16%, transparent);color:${color};border:1px solid color-mix(in srgb, ${color} 38%, transparent)`
    : 'color:var(--text-dim);background:var(--panel-soft)';
  const badge = isPrimary
    ? `<span class="oc-root-badge" title="Topo do organograma">Nível 0</span>`
    : (isOrphanRoot ? `<span class="oc-root-badge warn" title="Sem gestor vinculado na Convenia — provável falta de cadastro">⚠ Sem gestor</span>` : '');
  return `
    <div class="${cls.join(' ')}" data-uid="${escHtml(id)}">
      ${badge}
      <div class="oc-card-top" data-role="open">
        ${_ocPhotoBlock(p)}
        <div class="oc-card-info">
          <div class="oc-card-name">${escHtml(name)}${p.on_vacation ? ' <span title="Em férias">🏖</span>' : ''}</div>
          <div class="oc-card-role">${escHtml(p.cargo || p.team || '—')}</div>
        </div>
      </div>
      ${dept ? `<div class="oc-card-dept" style="${deptStyle}">${color ? `<span class="oc-dot" style="background:${color}"></span>` : ''}${escHtml(dept)}</div>` : ''}
      ${kids.length ? `<button type="button" class="oc-toggle${isExp ? ' expanded' : ''}" data-role="toggle" title="${isExp ? 'Recolher' : 'Expandir'} liderados">
          <span class="oc-toggle-arrow">▸</span> ${kids.length} liderado${kids.length > 1 ? 's' : ''}
        </button>` : ''}
    </div>`;
}

// Tentativa de quebrar liderados em várias fileiras (4+2, 5+5 etc.) foi
// revertida (2026-07-24): ao expandir um card no MEIO de uma fileira, a
// fileira de baixo não repaginava direito e a linha de conexão entre
// fileiras quebrava visualmente. Fileira única sempre é o que se mostrou
// confiável nos testes com dados reais — pan (arrastar) + zoom + rolagem
// horizontal (já existentes) cobrem o caso de muitos liderados.
function _ocChildrenBlock(id) {
  const kids = _oc.childrenOf[id] || [];
  if (!kids.length || !_oc.expanded.has(id)) return '';
  return `<ul class="oc-tree">${kids.map(c => `<li>${_ocNodeHtml(c, false)}</li>`).join('')}</ul>`;
}

function _ocNodeHtml(id, isRoot) {
  return _ocCardHtml(id, isRoot) + _ocChildrenBlock(id);
}

function _ocRender() {
  const wrap = document.getElementById('ocTreeWrap');
  if (!wrap) return;
  const visible = _ocVisibleRoots();
  if (!visible.length) {
    wrap.innerHTML = `<div class="empty-state">
      <h4>Nenhuma relação de gestor encontrada</h4>
      <p>Confirme se o campo Gestor está vindo preenchido da Convenia e se a sincronização já rodou<br>
      (Ajustes → Fonte de dados RH → Sincronizar agora).</p>
    </div>`;
    return;
  }
  wrap.innerHTML = `<ul class="oc-tree oc-roots">${visible.map(r => `<li>${_ocNodeHtml(r, true)}</li>`).join('')}</ul>`;
  _ocApplyZoom();
}

function _ocUpdateNote() {
  const el = document.getElementById('ocNote');
  if (!el) return;
  const total      = _oc.people.length;
  const withAgent  = _oc.people.filter(p => p.has_agent).length;
  const shownCount = _oc.shown.size; // alcançável a partir de TODAS as raízes
  const isolated   = total - shownCount; // sem gestor E sem liderados — nunca aparecem

  let orphanHidden = 0;
  if (_oc.hideOrphans) {
    _oc.roots.forEach(r => {
      if (r !== _oc.primaryRoot) orphanHidden += _ocSubtreeSize(r, _oc.childrenOf);
    });
  }

  let txt = `${total} colaborador${total === 1 ? '' : 'es'} da Convenia • ${withAgent} com agente WorkPulse`;
  if (orphanHidden > 0) {
    txt += ` • ${orphanHidden} sem gestor vinculado (ocultos — desmarque "Ocultar sem gestor" p/ ver)`;
  }
  if (isolated > 0) {
    txt += ` • ${isolated} sem nenhuma relação (não exibido${isolated === 1 ? '' : 's'})`;
  }
  el.textContent = txt;
}

function _ocUpdateLegend() {
  const el = document.getElementById('ocLegend');
  if (!el) return;
  const entries = Object.entries(_oc.deptColors);
  const deptHtml = entries.map(([d, c]) =>
    `<span class="oc-legend-item"><span class="oc-dot" style="background:${c}"></span>${escHtml(d)}</span>`
  ).join('');
  const agentHtml = `<span class="oc-legend-sep"></span>
    <span class="oc-legend-agent"><span class="oc-agent-dot yes"></span>Com agente</span>
    <span class="oc-legend-agent"><span class="oc-agent-dot no"></span>Sem agente</span>`;
  el.innerHTML = deptHtml + agentHtml;
  el.classList.remove('hide');
}

/* ---------- Mini-ficha local (colaborador sem agente — sem chamada de API) ---------- */
function _ocQuickRow(label, value) {
  return `<div class="wp-modal-row"><div class="wp-modal-label">${label}</div>
    <div class="wp-modal-value${value ? '' : ' muted'}">${value ? escHtml(value) : '—'}</div></div>`;
}
function _ocOpenQuickView(id) {
  const p = _oc.byId[id];
  if (!p) return;
  if (typeof closeProfileModal === 'function') closeProfileModal();
  const name = p.display_name || '—';
  const ov = document.createElement('div');
  ov.className = 'wp-modal-overlay';
  ov.id = 'wpProfileOverlay';
  ov.onclick = (e) => { if (e.target === ov && typeof closeProfileModal === 'function') closeProfileModal(); };
  ov.innerHTML = `
    <div class="wp-modal-card" role="dialog" aria-modal="true">
      <div class="wp-modal-head">
        ${_ocPhotoHtml(p, true)}
        <div class="wp-modal-title">
          <h3>${escHtml(name)}</h3>
          <span class="wp-modal-dept">${escHtml(p.cargo || p.department || '—')}</span>
        </div>
        <button class="wp-modal-close" title="Fechar (Esc)" onclick="closeProfileModal()">×</button>
      </div>
      <div class="wp-modal-body">
        ${_ocQuickRow('✉️ E-mail', p.email)}
        ${_ocQuickRow('👔 Gestor', p.manager)}
        ${_ocQuickRow('🏢 Departamento', p.department)}
        ${_ocQuickRow('👥 Time', p.team)}
      </div>
      <div class="oc-quick-foot">🔴 Sem agente WorkPulse instalado — dados vêm só da Convenia (sem ficha de atividade).</div>
    </div>`;
  document.body.appendChild(ov);
  if (typeof _wpProfileEsc === 'function') document.addEventListener('keydown', _wpProfileEsc);
}

/* ---------- Interação ---------- */
function _ocUpdateToggleBtn(btn, id) {
  if (!btn) return;
  const isExp = _oc.expanded.has(id);
  btn.classList.toggle('expanded', isExp);
  btn.title = (isExp ? 'Recolher' : 'Expandir') + ' liderados';
}

function _ocReducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

// Toggle "cirúrgico": em vez de re-renderizar a árvore inteira (o que fazia
// os cards aparecerem/sumirem secos, de uma vez), insere/remove só o <ul>
// de filhos daquele card específico e anima a entrada/saída. Mantém o resto
// da árvore intocado (evita repaginar/perder posição de scroll) — mais
// seguro do que reconstruir tudo, e como bônus fica bem mais suave.
function _ocToggle(id) {
  const card = document.querySelector(`.oc-card[data-uid="${CSS.escape(id)}"]`);
  const li = card && card.parentElement;
  if (!card || !li || li.tagName !== 'LI') {
    // fallback (card ainda não está no DOM por algum motivo) — comportamento antigo
    if (_oc.expanded.has(id)) _oc.expanded.delete(id); else _oc.expanded.add(id);
    _ocRender();
    return;
  }
  const btn = card.querySelector('[data-role="toggle"]');
  const willExpand = !_oc.expanded.has(id);
  if (willExpand) _oc.expanded.add(id); else _oc.expanded.delete(id);
  _ocUpdateToggleBtn(btn, id);

  const reduce = _ocReducedMotion();
  const existingUl = li.querySelector(':scope > ul.oc-tree');

  if (willExpand) {
    if (existingUl) return; // já presente (não deveria acontecer)
    const html = _ocChildrenBlock(id);
    if (!html) return;
    li.insertAdjacentHTML('beforeend', html);
    if (reduce) return;
    const ul = li.querySelector(':scope > ul.oc-tree');
    if (!ul) return;
    ul.querySelectorAll(':scope > li').forEach((kidLi, i) => {
      kidLi.animate(
        [
          { opacity: 0, transform: 'translateY(-10px) scale(.94)' },
          { opacity: 1, transform: 'translateY(0) scale(1)' },
        ],
        { duration: 260, delay: i * 45, easing: 'cubic-bezier(.25,.8,.35,1)', fill: 'backwards' }
      );
    });
  } else {
    if (!existingUl) return;
    if (reduce) { existingUl.remove(); return; }
    existingUl.style.pointerEvents = 'none';
    const anim = existingUl.animate(
      [
        { opacity: 1, transform: 'translateY(0) scale(1)' },
        { opacity: 0, transform: 'translateY(-8px) scale(.95)' },
      ],
      { duration: 170, easing: 'ease-in' }
    );
    anim.onfinish = () => existingUl.remove();
  }
}

function ocExpandAll() {
  _oc.expanded = new Set(Object.keys(_oc.childrenOf));
  _ocRender();
}
function ocCollapseAll() {
  _oc.expanded = new Set();
  _ocRender();
}
function ocSetLevels(sel) {
  const v = sel.value;
  _oc.levels = v;
  _oc.expanded = v === 'all' ? new Set(Object.keys(_oc.childrenOf)) : _ocDefaultExpand(parseInt(v, 10));
  _ocRender();
}
function ocToggleHideOrphans(chk) {
  _oc.hideOrphans = !!chk.checked;
  _ocRender();
  _ocUpdateNote();
}

let _ocSearchTimer = null;
function ocSearchInput(v) {
  clearTimeout(_ocSearchTimer);
  _ocSearchTimer = setTimeout(() => _ocApplySearch(v), 150);
}
function _ocApplySearch(qRaw) {
  const q = _ocNorm(qRaw);
  _oc.matches = new Set();
  if (q) {
    Object.values(_oc.byId).forEach(p => {
      if (!_oc.shown.has(p.id)) return;
      const hay = _ocNorm([p.display_name, p.cargo, p.department, p.team].filter(Boolean).join(' '));
      if (hay.includes(q)) _oc.matches.add(p.id);
    });
    // Achar um colaborador que estava oculto (raiz órfã) revela a árvore dele.
    if (_oc.matches.size && _oc.hideOrphans) {
      const anyOutsidePrimary = [..._oc.matches].some(id => {
        let cur = id;
        while (_oc.parentOf[cur]) cur = _oc.parentOf[cur];
        return cur !== _oc.primaryRoot;
      });
      if (anyOutsidePrimary) _oc.hideOrphans = false;
      const chk = document.getElementById('ocHideOrphans');
      if (chk) chk.checked = _oc.hideOrphans;
    }
    _oc.matches.forEach(id => {
      let cur = _oc.parentOf[id];
      while (cur) { _oc.expanded.add(cur); cur = _oc.parentOf[cur]; }
    });
  }
  _ocRender();
  if (q && _oc.matches.size) {
    requestAnimationFrame(() => {
      const first = [..._oc.matches][0];
      const el = document.querySelector(`.oc-card[data-uid="${CSS.escape(first)}"]`);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
    });
  }
}

function ocZoom(delta) {
  _oc.zoom = Math.min(1.15, Math.max(0.5, +(_oc.zoom + delta).toFixed(2)));
  _ocApplyZoom();
}
function ocZoomReset() { _oc.zoom = 1; _ocApplyZoom(); }
function _ocApplyZoom() {
  const el = document.getElementById('ocCanvas');
  if (el) el.style.transform = `scale(${_oc.zoom})`;
  const lbl = document.getElementById('ocZoomLbl');
  if (lbl) lbl.textContent = Math.round(_oc.zoom * 100) + '%';
}

/* ---------- Pan (clicar e arrastar p/ navegar, além da barra de rolagem) ---------- */
let _ocSuppressClick = false; // evita abrir ficha/expandir ao SOLTAR um arraste
function _ocWirePan() {
  const el = document.getElementById('ocScroll');
  if (!el || el.dataset.panWired) return;
  el.dataset.panWired = '1';
  const THRESH = 5; // px — abaixo disso ainda conta como clique, não arraste
  let dragging = false, moved = false, startX = 0, startY = 0, startLeft = 0, startTop = 0, pid = null;

  // IMPORTANTE: NÃO captura o ponteiro no pointerdown (isso quebrava o click
  // nos botões — expandir/recolher e abrir ficha paravam de funcionar, porque
  // um clique comum também é um pointerdown+pointerup). Só captura DEPOIS de
  // confirmar que é de fato um arraste (moved=true), no pointermove.
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return; // só botão esquerdo do mouse
    dragging = true; moved = false; pid = e.pointerId;
    startX = e.clientX; startY = e.clientY;
    startLeft = el.scrollLeft; startTop = el.scrollTop;
  });
  el.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX, dy = e.clientY - startY;
    if (!moved && (Math.abs(dx) > THRESH || Math.abs(dy) > THRESH)) {
      moved = true;
      el.classList.add('oc-panning');
      try { el.setPointerCapture(pid); } catch (_) { /* toque antigo sem suporte */ }
    }
    if (moved) {
      el.scrollLeft = startLeft - dx;
      el.scrollTop = startTop - dy;
    }
  });
  const endDrag = () => {
    if (dragging && moved) {
      _ocSuppressClick = true;
      setTimeout(() => { _ocSuppressClick = false; }, 0);
      try { el.releasePointerCapture(pid); } catch (_) { /* já solto */ }
    }
    dragging = false; moved = false;
    el.classList.remove('oc-panning');
  };
  el.addEventListener('pointerup', endDrag);
  el.addEventListener('pointercancel', endDrag);
  el.addEventListener('pointerleave', () => { if (!dragging) return; }); // pointer capture já cobre a maioria dos casos
}

function _ocWireEvents() {
  const wrap = document.getElementById('ocTreeWrap');
  if (!wrap || wrap.dataset.wired) return;
  wrap.dataset.wired = '1';
  wrap.addEventListener('click', (e) => {
    if (_ocSuppressClick) return; // clique disparado ao soltar um arraste — ignora
    const toggle = e.target.closest('[data-role="toggle"]');
    if (toggle) {
      const card = toggle.closest('.oc-card');
      const id = card && card.dataset.uid;
      if (id) _ocToggle(id);
      return;
    }
    const open = e.target.closest('[data-role="open"]');
    if (open) {
      const card = open.closest('.oc-card');
      const id = card && card.dataset.uid;
      const p = id && _oc.byId[id];
      if (!p) return;
      if (p.has_agent && p.user_id) {
        openProfileModal(p.user_id, p.display_name);
      } else {
        _ocOpenQuickView(id);
      }
    }
  });
}

/* ---------- Loader (chamado por wp-nav.js ao trocar de aba) ---------- */
async function loadOrgChart() {
  const wrap = document.getElementById('ocTreeWrap');
  if (wrap && !_oc.loaded) {
    wrap.innerHTML = `<div class="empty-state"><h4>Carregando organograma…</h4></div>`;
  }
  try {
    const data = await api(`/v1/org-chart?tenant=${encodeURIComponent(state.tenant)}`);
    const people = data.people || [];
    // Fotos grandes (data URI) agora vêm como referência (/v1/tenant-directory/photo?…)
    // em vez de embutidas no JSON (ver nota em server/main.py::org_chart). Path relativo
    // precisa apontar pro host da API (dashboard e API são domínios diferentes).
    people.forEach(p => {
      if (p.photo_url && p.photo_url.startsWith('/')) p.photo_url = state.url + p.photo_url;
    });
    const built = _ocBuildTree(people);
    _oc.people      = people;
    _oc.byId        = built.byId;
    _oc.childrenOf  = built.childrenOf;
    _oc.parentOf    = built.parentOf;
    _oc.roots       = built.roots;
    _oc.primaryRoot = built.primaryRoot;
    _oc.shown       = built.shown;
    _oc.deptColors  = _ocBuildDeptColors(people.filter(p => built.shown.has(p.id)));
    _oc.matches     = new Set();
    _oc.expanded    = _oc.levels === 'all'
      ? new Set(Object.keys(built.childrenOf))
      : _ocDefaultExpand(parseInt(_oc.levels, 10) || 2);
    _oc.loaded      = true;
    _ocWireEvents();
    _ocWirePan();
    _ocRender();
    _ocUpdateNote();
    _ocUpdateLegend();
    const searchEl = document.getElementById('ocSearch');
    if (searchEl) searchEl.value = '';
    const orphanChk = document.getElementById('ocHideOrphans');
    if (orphanChk) orphanChk.checked = _oc.hideOrphans;
  } catch (e) {
    if (wrap) wrap.innerHTML = `<div class="empty-state"><h4>Não foi possível carregar</h4><p>${escHtml(e.message)}</p></div>`;
  }
}
