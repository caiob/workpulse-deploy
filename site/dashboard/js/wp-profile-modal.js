// =====================================================================
// wp-profile-modal.js — Modal reutilizável: Ficha do Colaborador
// ---------------------------------------------------------------------
// Drop-in em QUALQUER página do painel. Basta chamar:
//     openProfileModal(userId, nomeOpcionalParaExibirNaHora);
//
// Mostra: Foto, Nome, E-mail, Nome da Máquina, Gestor e Departamento.
// Fonte: GET /v1/collaborator/profile (dados que já coletamos hoje).
// Foto e Gestor virão do Google Directory (sync noturno) na fase 2 —
// até lá aparecem como "sincroniza com Google (em breve)".
//
// Cache em memória: uma vez carregado o perfil de alguém, reutiliza na
// mesma sessão (o back-end já é atualizado 1x/dia pela carga noturna).
// =====================================================================

const _wpProfileCache = {};   // user_id -> dados do perfil

function closeProfileModal() {
  document.getElementById('wpProfileOverlay')?.remove();
  document.removeEventListener('keydown', _wpProfileEsc);
}
function _wpProfileEsc(e) { if (e.key === 'Escape') closeProfileModal(); }

// Procura dados que já estão no cliente (evita tela vazia se o back-end
// ainda não tiver o endpoint publicado, ou como preenchimento imediato).
function _wpProfileClientData(userId) {
  const pools = [state.collabs, state.people, state.teamUsers].filter(Array.isArray);
  for (const pool of pools) {
    const u = pool.find(x => x && x.user_id === userId);
    if (u) return u;
  }
  return null;
}

function _wpInitial(name) {
  const s = (name || '?').replace(/[^A-Za-zÀ-ÿ0-9 ]/g, ' ').trim().split(/\s+/);
  return ((s[0]?.[0] || '') + (s[1]?.[0] || '')).toUpperCase() || '?';
}

function _wpModalPhoto(p, name) {
  if (p && p.photo_url) {
    return `<img class="wp-modal-photo" src="${escHtml(p.photo_url)}" alt="${escHtml(name)}"
      onerror="this.outerHTML='<div class=\\'wp-modal-photo lg\\'>${_wpInitial(name)}</div>'">`;
  }
  return `<div class="wp-modal-photo lg">${_wpInitial(name)}</div>`;
}

function _wpRow(label, value, muted) {
  const cls = muted ? 'wp-modal-value muted' : 'wp-modal-value';
  return `<div class="wp-modal-row">
    <div class="wp-modal-label">${label}</div>
    <div class="${cls}">${value}</div>
  </div>`;
}

function _wpSkelRow(label) {
  return `<div class="wp-modal-row">
    <div class="wp-modal-label">${label}</div>
    <div class="wp-modal-value" style="flex:1"><div class="wp-modal-skeleton" style="width:70%"></div></div>
  </div>`;
}

// Monta a casca do modal (com skeleton) e devolve o container de conteúdo.
function _wpRenderShell(name, dept) {
  closeProfileModal();
  const ov = document.createElement('div');
  ov.className = 'wp-modal-overlay';
  ov.id = 'wpProfileOverlay';
  ov.onclick = (e) => { if (e.target === ov) closeProfileModal(); };
  ov.innerHTML = `
    <div class="wp-modal-card" role="dialog" aria-modal="true">
      <div class="wp-modal-head">
        <div id="wpProfilePhoto">${_wpModalPhoto(null, name)}</div>
        <div class="wp-modal-title">
          <h3 id="wpProfileName">${escHtml(name || 'Colaborador')}</h3>
          <span class="wp-modal-dept" id="wpProfileDept">${escHtml(dept || '—')}</span>
        </div>
        <button class="wp-modal-close" title="Fechar (Esc)" onclick="closeProfileModal()">×</button>
      </div>
      <div class="wp-modal-body" id="wpProfileBody">
        ${_wpSkelRow('✉️ E-mail')}
        ${_wpSkelRow('💻 Máquina')}
        ${_wpSkelRow('👔 Gestor')}
        ${_wpSkelRow('🏢 Departamento')}
        ${_wpSkelRow('👥 Time')}
      </div>
    </div>`;
  document.body.appendChild(ov);
  document.addEventListener('keydown', _wpProfileEsc);
}

function _wpFillModal(p, fallbackName) {
  const name = p.display_name || fallbackName || cleanUserId(p.user_id);
  const dept = p.department || 'Sem departamento';
  document.getElementById('wpProfilePhoto').innerHTML = _wpModalPhoto(p, name);
  document.getElementById('wpProfileName').textContent = name;
  // Abaixo do nome/foto mostra o CARGO (fallback: departamento).
  document.getElementById('wpProfileDept').textContent = p.cargo || dept || '—';

  const googleHint = '<span title="Virá do diretório de RH (Google Workspace ou Convenia) na sincronização noturna">sincroniza com RH (em breve)</span>';
  const body = document.getElementById('wpProfileBody');
  body.innerHTML =
    _wpRow('✉️ E-mail',       p.email    ? escHtml(p.email)    : '—', !p.email) +
    _wpRow('💻 Máquina',      p.hostname ? escHtml(p.hostname) : '—', !p.hostname) +
    _wpRow('👔 Gestor',       p.manager  ? escHtml(p.manager)  : googleHint, !p.manager) +
    _wpRow('🏢 Departamento', p.department ? escHtml(dept) : '—', !p.department) +
    _wpRow('👥 Time',         p.team     ? escHtml(p.team)     : googleHint, !p.team);

  // Rodapé: aviso só quando falta algo que depende do Google.
  if (!p.photo_url || !p.manager) {
    const foot = document.createElement('div');
    foot.className = 'wp-modal-foot';
    foot.textContent = 'Foto e gestor são sincronizados do diretório de RH (Google Workspace ou Convenia — carga diária). Pendentes até a integração ser configurada.';
    body.parentElement.appendChild(foot);
  }
}

async function openProfileModal(userId, name) {
  if (!userId) return;
  const client = _wpProfileClientData(userId);
  _wpRenderShell(name || (client && displayName(client)) || '', client && (client.cargo || client.department));

  // Cache de sessão: se já buscamos, mostra na hora.
  if (_wpProfileCache[userId]) { _wpFillModal(_wpProfileCache[userId], name); return; }

  try {
    const p = await api('/v1/collaborator/profile?tenant=' + encodeURIComponent(state.tenant) +
      '&user_id=' + encodeURIComponent(userId));
    _wpProfileCache[userId] = p;
    if (document.getElementById('wpProfileOverlay')) _wpFillModal(p, name);
  } catch (e) {
    // Back-end antigo/sem endpoint: cai para os dados que já temos no cliente.
    if (!document.getElementById('wpProfileOverlay')) return;
    if (client) {
      _wpFillModal({
        user_id:      userId,
        display_name: displayName(client),
        email:        client.email,
        department:   client.department,
        hostname:     client.hostname || null,
        manager:      null,
        photo_url:    null,
      }, name);
    } else {
      const body = document.getElementById('wpProfileBody');
      if (body) body.innerHTML = `<div class="wp-modal-row"><div class="wp-modal-value muted">Não foi possível carregar a ficha: ${escHtml(e.message)}</div></div>`;
    }
  }
}
