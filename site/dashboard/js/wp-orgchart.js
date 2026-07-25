// =====================================================================
// wp-orgchart.js — Análises → Organograma
// ---------------------------------------------------------------------
// Árvore hierárquica interativa a partir de GET /v1/org-chart (nome, e-mail,
// departamento, gestor (texto vindo da Convenia), cargo, time, foto).
//
// Montagem da árvore: casa `manager` (texto) de cada colaborador com o
// `display_name` de outro colaborador do mesmo tenant.
//
// Regra de raiz (definida com o Caio 2026-07-24): a Convenia é a fonte de
// verdade do campo Gestor. Quem tem gestor vinculado aparece como filho
// dele. Quem NÃO tem gestor (vazio ou sem correspondência) só aparece no
// organograma se tiver pelo menos 1 liderado — nesse caso vira uma "raiz"
// (ex.: CTO/CEO). Quem não tem gestor E não lidera ninguém (sem nenhuma
// relação) fica de fora — não polui o topo com nós soltos.
//
// Interação: clique na foto/nome abre a Ficha do Colaborador (modal já
// existente, wp-profile-modal.js). Clique no chevron expande/recolhe os
// liderados daquele nó. Busca por nome/cargo/departamento expande o
// caminho até os resultados. Zoom (+/−) e "Níveis exibidos" controlam
// quanto aparece de uma vez, para caber na página sem "explodir".
// =====================================================================

const OC_PALETTE = ['var(--primary)', 'var(--accent)', 'var(--violet)', 'var(--teal)', 'var(--pink)', 'var(--warn)'];

const _oc = {
  people:      [],
  byId:        {},
  childrenOf:  {},
  parentOf:    {},
  roots:       [],
  shown:       new Set(),
  deptColors:  {},
  expanded:    new Set(),
  matches:     new Set(),
  levels:      3,
  zoom:        1,
  loaded:      false,
};

/* ---------- Normalização p/ casar manager (texto) com display_name ---------- */
function _ocNorm(s) {
  return (s || '')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/\s+/g, ' ').trim();
}

function _ocBuildTree(people) {
  const byId = {};
  const byNameNorm = {};
  people.forEach(p => {
    byId[p.user_id] = p;
    const key = _ocNorm(p.display_name || '');
    if (key && !(key in byNameNorm)) byNameNorm[key] = p.user_id;
  });

  const childrenOf = {};
  const parentOf = {};
  people.forEach(p => {
    const mgrKey = _ocNorm(p.manager || '');
    const parentId = mgrKey ? byNameNorm[mgrKey] : null;
    if (parentId && parentId !== p.user_id) {
      parentOf[p.user_id] = parentId;
      (childrenOf[parentId] = childrenOf[parentId] || []).push(p.user_id);
    } else {
      parentOf[p.user_id] = null;
    }
  });

  const byName = (a, b) => (byId[a].display_name || '').localeCompare(byId[b].display_name || '', 'pt-BR');
  Object.values(childrenOf).forEach(arr => arr.sort(byName));

  const roots = people
    .filter(p => !parentOf[p.user_id] && (childrenOf[p.user_id] || []).length > 0)
    .map(p => p.user_id)
    .sort(byName);

  const shown = new Set();
  function walk(id) {
    if (shown.has(id)) return; // guarda contra ciclo
    shown.add(id);
    (childrenOf[id] || []).forEach(walk);
  }
  roots.forEach(walk);

  return { byId, childrenOf, parentOf, roots, shown };
}

function _ocDefaultExpand(levels) {
  const exp = new Set();
  function walk(id, depth) {
    if (depth < levels - 1) {
      exp.add(id);
      (_oc.childrenOf[id] || []).forEach(c => walk(c, depth + 1));
    }
  }
  _oc.roots.forEach(r => walk(r, 0));
  return exp;
}

function _ocBuildDeptColors(people) {
  const depts = [...new Set(people.map(p => (p.department || '').trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, 'pt-BR'));
  const map = {};
  depts.forEach((d, i) => { map[d] = OC_PALETTE[i % OC_PALETTE.length]; });
  return map;
}

/* ---------- Render ---------- */
function _ocPhotoHtml(p) {
  const name = p.display_name || cleanUserId(p.user_id);
  if (p.photo_url) {
    return `<img class="oc-photo" src="${escHtml(p.photo_url)}" alt="${escHtml(name)}" loading="lazy"
      onerror="this.outerHTML='&lt;div class=&quot;oc-photo oc-photo-fallback&quot;&gt;${_wpInitial(name)}&lt;/div&gt;'">`;
  }
  return `<div class="oc-photo oc-photo-fallback">${_wpInitial(name)}</div>`;
}

function _ocCardHtml(id, isRoot) {
  const p = _oc.byId[id];
  const name = p.display_name || cleanUserId(p.user_id);
  const dept = (p.department || '').trim();
  const color = dept ? (_oc.deptColors[dept] || 'var(--text-muted)') : null;
  const kids = _oc.childrenOf[id] || [];
  const isExp = _oc.expanded.has(id);
  const isMatch = _oc.matches.has(id);
  return `
    <div class="oc-card${isRoot ? ' root' : ''}${isMatch ? ' match' : ''}" data-uid="${escHtml(id)}">
      <div class="oc-card-top" data-role="open">
        ${_ocPhotoHtml(p)}
        <div class="oc-card-info">
          <div class="oc-card-name">${escHtml(name)}${p.on_vacation ? ' <span title="Em férias">🏖</span>' : ''}</div>
          <div class="oc-card-role">${escHtml(p.cargo || p.team || '—')}</div>
        </div>
      </div>
      ${dept ? `<div class="oc-card-dept"><span class="oc-dot" style="background:${color}"></span>${escHtml(dept)}</div>` : ''}
      ${kids.length ? `<button type="button" class="oc-toggle" data-role="toggle" title="${isExp ? 'Recolher' : 'Expandir'} liderados">
          <span class="oc-toggle-arrow">${isExp ? '▾' : '▸'}</span> ${kids.length} liderado${kids.length > 1 ? 's' : ''}
        </button>` : ''}
    </div>`;
}

function _ocLiHtml(id, isRoot) {
  const kids = _oc.childrenOf[id] || [];
  const isExp = _oc.expanded.has(id);
  const childrenHtml = (kids.length && isExp)
    ? `<ul class="oc-tree">${kids.map(c => _ocLiHtml(c, false)).join('')}</ul>`
    : '';
  return `<li>${_ocCardHtml(id, isRoot)}${childrenHtml}</li>`;
}

function _ocRender() {
  const wrap = document.getElementById('ocTreeWrap');
  if (!wrap) return;
  if (!_oc.roots.length) {
    wrap.innerHTML = `<div class="empty-state">
      <h4>Nenhuma relação de gestor encontrada</h4>
      <p>Confirme se o campo Gestor está vindo preenchido da Convenia e se a sincronização já rodou<br>
      (Ajustes → Fonte de dados RH → Sincronizar agora).</p>
    </div>`;
    return;
  }
  wrap.innerHTML = `<ul class="oc-tree oc-roots">${_oc.roots.map(r => _ocLiHtml(r, true)).join('')}</ul>`;
  _ocApplyZoom();
}

function _ocUpdateNote() {
  const el = document.getElementById('ocNote');
  if (!el) return;
  const total = _oc.people.length;
  const shownCount = _oc.shown.size;
  const hidden = total - shownCount;
  el.textContent = hidden > 0
    ? `${shownCount} colaborador${shownCount === 1 ? '' : 'es'} no organograma • ${hidden} sem gestor vinculado e sem liderados diretos (não exibido${hidden === 1 ? '' : 's'})`
    : `${shownCount} colaborador${shownCount === 1 ? '' : 'es'} no organograma`;
}

function _ocUpdateLegend() {
  const el = document.getElementById('ocLegend');
  if (!el) return;
  const entries = Object.entries(_oc.deptColors);
  if (!entries.length) { el.innerHTML = ''; el.classList.add('hide'); return; }
  el.classList.remove('hide');
  el.innerHTML = entries.map(([d, c]) =>
    `<span class="oc-legend-item"><span class="oc-dot" style="background:${c}"></span>${escHtml(d)}</span>`
  ).join('');
}

/* ---------- Interação ---------- */
function _ocToggle(id) {
  if (_oc.expanded.has(id)) _oc.expanded.delete(id); else _oc.expanded.add(id);
  _ocRender();
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
      if (!_oc.shown.has(p.user_id)) return;
      const hay = _ocNorm([p.display_name, p.cargo, p.department, p.team].filter(Boolean).join(' '));
      if (hay.includes(q)) _oc.matches.add(p.user_id);
    });
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
  _oc.zoom = Math.min(1.15, Math.max(0.55, +(_oc.zoom + delta).toFixed(2)));
  _ocApplyZoom();
}
function ocZoomReset() { _oc.zoom = 1; _ocApplyZoom(); }
function _ocApplyZoom() {
  const el = document.getElementById('ocCanvas');
  if (el) el.style.transform = `scale(${_oc.zoom})`;
  const lbl = document.getElementById('ocZoomLbl');
  if (lbl) lbl.textContent = Math.round(_oc.zoom * 100) + '%';
}

function _ocWireEvents() {
  const wrap = document.getElementById('ocTreeWrap');
  if (!wrap || wrap.dataset.wired) return;
  wrap.dataset.wired = '1';
  wrap.addEventListener('click', (e) => {
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
      if (id) openProfileModal(id, p ? p.display_name : null);
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
    const built = _ocBuildTree(people);
    _oc.people      = people;
    _oc.byId        = built.byId;
    _oc.childrenOf  = built.childrenOf;
    _oc.parentOf    = built.parentOf;
    _oc.roots       = built.roots;
    _oc.shown       = built.shown;
    _oc.deptColors  = _ocBuildDeptColors(people.filter(p => built.shown.has(p.user_id)));
    _oc.matches     = new Set();
    _oc.expanded    = _oc.levels === 'all'
      ? new Set(Object.keys(built.childrenOf))
      : _ocDefaultExpand(parseInt(_oc.levels, 10) || 3);
    _oc.loaded      = true;
    _ocWireEvents();
    _ocRender();
    _ocUpdateNote();
    _ocUpdateLegend();
    const searchEl = document.getElementById('ocSearch');
    if (searchEl) searchEl.value = '';
  } catch (e) {
    if (wrap) wrap.innerHTML = `<div class="empty-state"><h4>Não foi possível carregar</h4><p>${escHtml(e.message)}</p></div>`;
  }
}
