// =====================================================================
// wp-nav.js — Navegação por abas com dropdowns
//
// Estrutura:
//   Visão Geral  |  [Coletas ▾]  |  [Análises ▾]
//   Coletas  → Pessoas, Departamentos, Eventos, Internet, Jornada de Trabalho
//   Análises → Alertas, Tendências
//
//   ACTIONS (topo direito):
//   status • tenant • [⚙ ▾ → Configurações / Sair]
// =====================================================================

const TABS = ['overview', 'people', 'departments', 'alerts', 'trends', 'events', 'internet', 'jornada', 'localizacao', 'financeiro', 'admusuarios', 'admlicenca', 'categorizacao', 'colaboradores', 'relatorios', 'config'];

// Mapeamento tab → grupo de dropdown (null = standalone)
const TAB_GROUP = {
  people:      'coletas',
  departments: 'coletas',
  events:      'coletas',
  internet:    'coletas',
  jornada:     'coletas',
  localizacao: 'coletas',
  financeiro:  'administrativo',
  admusuarios: 'administrativo',
  admlicenca:  'administrativo',
  categorizacao:'administrativo',
  colaboradores:'administrativo',
  relatorios:  'administrativo',
  alerts:      'analises',
  trends:      'analises',
};

function switchTab(name) {
  _cancelPending();

  // Oculta todas as seções
  TABS.forEach(t => {
    const sec = document.getElementById('tab-' + t);
    if (sec) sec.classList.add('hide');
  });

  // Exibe a seção alvo
  const target = document.getElementById('tab-' + name);
  if (target) target.classList.remove('hide');

  // Atualiza botões standalone
  document.querySelectorAll('nav.tabs > button[data-tab]').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });

  // Atualiza triggers de dropdown e itens ativos dentro deles
  document.querySelectorAll('.nav-dropdown[data-group]').forEach(dd => {
    const group     = dd.dataset.group;
    const isActive  = TAB_GROUP[name] === group;
    const trigger   = dd.querySelector('.nav-dd-trigger');
    if (trigger) trigger.classList.toggle('active', isActive);
    dd.querySelectorAll('.nav-dropdown-menu button[data-tab]').forEach(b => {
      b.classList.toggle('active-item', b.dataset.tab === name);
    });
  });

  // Fecha qualquer dropdown aberto
  document.querySelectorAll('.nav-dropdown.open').forEach(d => d.classList.remove('open'));

  // Carrega conteúdo da aba
  const loaders = {
    overview:    loadOverview,
    people:      loadPeople,
    departments: loadDepartments,
    alerts:      loadAlerts,
    trends:      loadTrends,
    events:      loadEvents,
    internet:    loadInternet,
    jornada:     loadJornada,
    localizacao: loadLocations,
    financeiro:  () => { if (typeof loadFinance === 'function') loadFinance(); },
    admusuarios: loadUsers,
    admlicenca:  loadLicense,
    categorizacao: () => { if (typeof loadRules === 'function') loadRules(); if (typeof loadAppsForConfig === 'function') loadAppsForConfig(); if (typeof loadSitesForConfig === 'function') loadSitesForConfig(); },
    colaboradores: () => { if (typeof loadDepts === 'function') loadDepts(); if (typeof loadCollabs === 'function') loadCollabs(); },
    relatorios:  () => { if (typeof loadReports === 'function') loadReports(); },
    config:      loadConfig,
  };
  if (loaders[name]) loaders[name]();
}

// ---- Wiring dos botões ----
// (scripts com defer: DOM já disponível ao executar)
(function initNav() {
  // Botões de aba (standalone e dentro de dropdowns)
  document.querySelectorAll('[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      if (tab) switchTab(tab);
    });
  });

  // Click nos triggers de dropdown abre/fecha o menu
  document.querySelectorAll('.nav-dd-trigger').forEach(trigger => {
    trigger.addEventListener('click', e => {
      e.stopPropagation();
      const dd     = trigger.closest('.nav-dropdown');
      if (!dd) return;
      const isOpen = dd.classList.contains('open');
      // Fecha todos antes de abrir outro
      document.querySelectorAll('.nav-dropdown.open').forEach(d => d.classList.remove('open'));
      if (!isOpen) dd.classList.add('open');
    });
  });

  // Click fora fecha todos os dropdowns
  document.addEventListener('click', () => {
    document.querySelectorAll('.nav-dropdown.open').forEach(d => d.classList.remove('open'));
  });

  // Previne que clique dentro do menu feche o dropdown
  document.querySelectorAll('.nav-dropdown-menu').forEach(menu => {
    menu.addEventListener('click', e => e.stopPropagation());
  });

  // ── Inicializa os date pickers de todas as abas ──────────────────
  wpDP.init('rangeDays',   { defaultDays: 7,  onChange: () => { loadOverview(); loadAlerts(); } });
  wpDP.init('peopleRange', { defaultDays: 7,  onChange: loadPeople });
  wpDP.init('trendDays',   { defaultDays: 30, onChange: loadTrends });
  wpDP.init('evDays',      { defaultDays: 1,  onChange: loadEvents });
  wpDP.init('intDays',     { defaultDays: 1,  onChange: loadInternet });
  wpDP.init('jorDays',     { defaultDays: 7,  onChange: loadJornada });
})();
