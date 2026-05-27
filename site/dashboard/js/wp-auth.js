// =====================================================================
// wp-auth.js — Autenticação, sessão, tema claro/escuro
// Carregado por último: depende de todas as outras funções (switchTab,
// loadOverview, etc.) que já estão no escopo global.
// =====================================================================

/* ==================== TEMA ==================== */

function applyTheme(theme) {
  document.body.classList.toggle('theme-light', theme === 'light');

  // Atualiza botão de toggle em Configurações (se visível)
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = theme === 'light' ? '🌙 Tema Escuro' : '☀️ Tema Claro';

  // Destrói charts em cache para recriá-los com as cores corretas do tema
  Object.keys(state.charts).forEach(k => {
    if (state.charts[k] && typeof state.charts[k].destroy === 'function') {
      state.charts[k].destroy();
    }
    delete state.charts[k];
  });

  // Recarrega a aba ativa para redesenhar os charts com o novo tema
  if (state.token) {
    const activeBtn  = document.querySelector('nav.tabs > button.active');
    const activeItem = document.querySelector('.nav-dropdown-menu .active-item');
    const current    = activeItem || activeBtn;
    if (current && current.dataset && current.dataset.tab) {
      switchTab(current.dataset.tab);
    }
  }
}

function toggleTheme() {
  const isLight = document.body.classList.contains('theme-light');
  const next    = isLight ? 'dark' : 'light';
  localStorage.setItem('wp_theme', next);
  applyTheme(next);
  // Atualiza botão imediatamente
  const btn = document.getElementById('themeToggleBtn');
  if (btn) btn.textContent = next === 'light' ? '🌙 Tema Escuro' : '☀️ Tema Claro';
}

/* ==================== LOGIN ==================== */

function _showLoginError(msg) {
  const el = document.getElementById('loginError');
  if (el) { el.textContent = msg; el.style.display = 'block'; }
}

function _clearLoginError() {
  const el = document.getElementById('loginError');
  if (el) el.style.display = 'none';
}

async function doLogin() {
  _clearLoginError();

  const url    = document.getElementById('loginUrl').value.trim().replace(/\/$/, '');
  const tenant = document.getElementById('loginTenant').value.trim();
  const token  = document.getElementById('loginToken').value.trim();

  if (!url || !tenant || !token) {
    _showLoginError('Preencha todos os campos para continuar.');
    return;
  }

  const btn          = document.querySelector('#login button.primary');
  const originalText = btn.textContent;
  btn.textContent = 'Verificando...';
  btn.disabled    = true;

  try {
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), 12000); // timeout 12s

    const r = await fetch(
      url + '/v1/workforce/overview?tenant=' + encodeURIComponent(tenant) + '&days=1',
      { headers: { 'X-Admin-Token': token }, signal: controller.signal }
    );
    clearTimeout(tid);

    if (r.status === 401 || r.status === 403) {
      _showLoginError('Token inválido ou sem permissão. Verifique suas credenciais.');
      return;
    }

    // Qualquer resposta que não seja 401/403 significa que as credenciais
    // são válidas (pode ser 200, 500 de erro de servidor, etc.)

    // Salva e entra
    state.url    = url;
    state.tenant = tenant;
    state.token  = token;
    localStorage.setItem('wp_url',    url);
    localStorage.setItem('wp_tenant', tenant);
    localStorage.setItem('wp_token',  token);

    document.getElementById('login').classList.add('hide');
    document.getElementById('tenantPill').textContent = tenant;
    loadOverview();

  } catch (e) {
    if (e.name === 'AbortError') {
      _showLoginError('Servidor não respondeu. Verifique o Endpoint da API.');
    } else {
      _showLoginError('Erro de conexão: ' + e.message);
    }
  } finally {
    btn.textContent = originalText;
    btn.disabled    = false;
  }
}

function logout() {
  localStorage.removeItem('wp_token');
  state.token = '';
  document.getElementById('login').classList.remove('hide');
  _clearLoginError();
}

/* ==================== BOOTSTRAP ==================== */
(function bootstrap() {
  // Aplica tema salvo
  const savedTheme = localStorage.getItem('wp_theme') || 'light';
  applyTheme(savedTheme);

  // Restaura sessão salva
  const url    = localStorage.getItem('wp_url');
  const tenant = localStorage.getItem('wp_tenant');
  const token  = localStorage.getItem('wp_token');

  if (url && tenant && token) {
    document.getElementById('loginUrl').value    = url;
    document.getElementById('loginTenant').value = tenant;
    document.getElementById('loginToken').value  = token;
    state.url    = url;
    state.tenant = tenant;
    state.token  = token;
    document.getElementById('login').classList.add('hide');
    document.getElementById('tenantPill').textContent = tenant;
    loadOverview();
  }
})();
