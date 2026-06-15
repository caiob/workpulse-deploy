// =====================================================================
// wp-reports.js — Gerenciamento de Relatórios Automáticos
// Aba "Configurações" › card "Relatórios Automáticos"
// =====================================================================

let _reportsList = [];   // cache local dos configs de relatório

const FREQ_LABELS = {
  daily:   'Diário',
  weekly:  'Semanal (seg)',
  monthly: 'Mensal (dia 1)',
};

const SECTION_LABELS = {
  coletas:    'Coletas por Pessoa',
  jornada:    'Jornada de Trabalho',
  tendencias: 'Tendências',
  alertas:    'Alertas',
};

// Tipos de relatório oferecidos na criação (1 por vez).
const REPORT_TYPES = {
  jornada: 'Jornada de Trabalho',
  alertas: 'Alertas',
};

/* ============================================================
   CARREGAMENTO PRINCIPAL
   ============================================================ */
async function loadReports() {
  try {
    const data = await api('/v1/reports?tenant=' + encodeURIComponent(state.tenant));
    _reportsList = data.reports || [];
    renderReportsList(_reportsList);
  } catch (e) {
    const el = document.getElementById('reportsListContainer');
    if (el) el.innerHTML = `<p class="empty-state">Erro ao carregar relatórios: ${e.message}</p>`;
  }
}

/* ============================================================
   RENDERIZAÇÃO DA LISTA
   ============================================================ */
function renderReportsList(reports) {
  const container = document.getElementById('reportsListContainer');
  if (!container) return;

  if (!reports.length) {
    container.innerHTML = `
      <p class="empty-state">
        Nenhum relatório configurado ainda.<br>
        <small>Clique em <strong>+ Novo Relatório</strong> para criar o primeiro.</small>
      </p>`;
    return;
  }

  container.innerHTML = reports.map(r => {
    const recipients = (typeof r.recipients === 'string' ? JSON.parse(r.recipients) : r.recipients) || [];
    const sections   = (typeof r.sections  === 'string' ? JSON.parse(r.sections)   : r.sections)   || [];
    const sectionStr = sections.map(s => SECTION_LABELS[s] || s).join(', ');
    const statusBadge = r.enabled
      ? '<span class="badge badge-ok">Ativo</span>'
      : '<span class="badge badge-warn">Pausado</span>';

    return `
      <div class="report-card" data-id="${r.id}">
        <div class="report-card-header">
          <span class="report-card-title">${_esc(r.name)}</span>
          ${statusBadge}
        </div>
        <div class="report-card-meta">
          <span title="Frequência">🕐 ${FREQ_LABELS[r.frequency] || r.frequency}</span>
          <span title="Destinatários">✉️ ${recipients.length} destinatário${recipients.length !== 1 ? 's' : ''}</span>
        </div>
        <div class="report-card-sections">
          <small>${_esc(sectionStr)}</small>
        </div>
        <div class="report-card-recipients">
          <small style="color:var(--text-dim)">${recipients.map(_esc).join(' · ')}</small>
        </div>
        <div class="report-card-actions">
          <button class="btn btn-sm btn-outline" onclick="previewReport(${r.id}, '${_esc(r.name)}', this)">👁 Pré-visualizar</button>
          <button class="btn btn-sm btn-outline" onclick="openEditReportModal(${r.id})">✏️ Editar</button>
          <button class="btn btn-sm btn-outline" onclick="showReportHistory(${r.id}, '${_esc(r.name)}')">📋 Histórico</button>
          <button class="btn btn-sm btn-outline" onclick="sendReportNow(${r.id}, '${_esc(r.name)}')">▶ Enviar Agora</button>
          <button class="btn btn-sm btn-danger-outline" onclick="deleteReport(${r.id}, '${_esc(r.name)}')">🗑 Excluir</button>
        </div>
      </div>`;
  }).join('');
}

/* ============================================================
   MODAL CRIAR / EDITAR
   ============================================================ */
function openCreateReportModal() {
  _openReportModal(null);
}

function openEditReportModal(id) {
  const cfg = _reportsList.find(r => r.id === id);
  if (!cfg) { toast('Relatório não encontrado'); return; }
  _openReportModal(cfg);
}

function _openReportModal(cfg) {
  const isEdit = !!cfg;

  // Remove modal anterior se existir
  document.getElementById('reportModal')?.remove();

  const recipients = cfg
    ? (typeof cfg.recipients === 'string' ? JSON.parse(cfg.recipients) : cfg.recipients)
    : [];
  const sections = cfg
    ? (typeof cfg.sections === 'string' ? JSON.parse(cfg.sections) : cfg.sections)
    : ['jornada'];

  // Tipo atual = primeiro tipo válido do config (default: jornada).
  const curType = (sections.includes('alertas') && !sections.includes('jornada')) ? 'alertas' : 'jornada';
  const typeOpts = Object.entries(REPORT_TYPES).map(([key, label]) =>
    `<option value="${key}" ${curType === key ? 'selected' : ''}>${label}</option>`).join('');

  const modal = document.createElement('div');
  modal.id = 'reportModal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-box" style="max-width:520px">
      <h3 class="modal-title">${isEdit ? '✏️ Editar Relatório' : '➕ Novo Relatório'}</h3>

      <div class="form-group">
        <label>Nome do Relatório</label>
        <input id="rptName" class="input" type="text" maxlength="80"
          placeholder="Ex: Relatório Semanal de TI"
          value="${_esc(cfg?.name || '')}">
      </div>

      <div class="form-group">
        <label>Frequência de Envio</label>
        <select id="rptFreq" class="input">
          <option value="daily"   ${cfg?.frequency === 'daily'   ? 'selected' : ''}>Diário (todos os dias às 07h BRT)</option>
          <option value="weekly"  ${cfg?.frequency === 'weekly'  ? 'selected' : ''}>Semanal (toda segunda às 07h BRT)</option>
          <option value="monthly" ${cfg?.frequency === 'monthly' ? 'selected' : ''}>Mensal (dia 1 às 07h BRT)</option>
        </select>
      </div>

      <div class="form-group">
        <label>Tipo de relatório</label>
        <select id="rptType" class="input">${typeOpts}</select>
      </div>

      <div class="form-group">
        <label>Destinatários <small style="color:var(--text-dim)">(um e-mail por linha)</small></label>
        <textarea id="rptRecipients" class="input" rows="3"
          placeholder="gestor@empresa.com&#10;diretoria@empresa.com"
          style="resize:vertical">${recipients.join('\n')}</textarea>
      </div>

      <div class="form-group" style="display:flex;align-items:center;gap:10px">
        <label style="margin:0">Relatório ativo</label>
        <input id="rptEnabled" type="checkbox" ${(cfg?.enabled !== false) ? 'checked' : ''}>
      </div>

      <div class="modal-actions">
        <button class="btn btn-outline" onclick="document.getElementById('reportModal').remove()">Cancelar</button>
        <button class="btn btn-primary" onclick="${isEdit ? `saveEditReport(${cfg.id})` : 'saveNewReport()'}">
          ${isEdit ? 'Salvar Alterações' : 'Criar Relatório'}
        </button>
      </div>
    </div>`;

  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.getElementById('rptName').focus();
}

function _collectReportForm() {
  const name = document.getElementById('rptName').value.trim();
  const frequency = document.getElementById('rptFreq').value;
  const enabled = document.getElementById('rptEnabled').checked;
  const recipientsRaw = document.getElementById('rptRecipients').value;
  const recipients = recipientsRaw.split('\n').map(s => s.trim()).filter(s => s.includes('@'));
  const type = document.getElementById('rptType').value;
  const sections = type ? [type] : [];

  if (!name) { toast('Informe o nome do relatório.'); return null; }
  if (!recipients.length) { toast('Informe ao menos um e-mail válido.'); return null; }
  if (!sections.length) { toast('Selecione o tipo de relatório.'); return null; }

  return { name, frequency, recipients, sections, enabled };
}

async function saveNewReport() {
  const body = _collectReportForm();
  if (!body) return;
  try {
    await apiFetch('POST', '/v1/reports?tenant=' + encodeURIComponent(state.tenant), body);
    document.getElementById('reportModal')?.remove();
    toast('Relatório criado com sucesso!');
    await loadReports();
  } catch (e) {
    toast('Erro ao criar relatório: ' + e.message);
  }
}

async function saveEditReport(id) {
  const body = _collectReportForm();
  if (!body) return;
  try {
    await apiFetch('PUT', `/v1/reports/${id}?tenant=` + encodeURIComponent(state.tenant), body);
    document.getElementById('reportModal')?.remove();
    toast('Relatório atualizado!');
    await loadReports();
  } catch (e) {
    toast('Erro ao salvar relatório: ' + e.message);
  }
}

/* ============================================================
   EXCLUIR
   ============================================================ */
async function deleteReport(id, name) {
  if (!confirm(`Excluir o relatório "${name}"? Esta ação não pode ser desfeita.`)) return;
  try {
    await apiFetch('DELETE', `/v1/reports/${id}?tenant=` + encodeURIComponent(state.tenant));
    toast('Relatório excluído.');
    await loadReports();
  } catch (e) {
    toast('Erro ao excluir: ' + e.message);
  }
}

/* ============================================================
   ENVIAR AGORA (teste)
   ============================================================ */
async function sendReportNow(id, name) {
  if (!confirm(`Enviar o relatório "${name}" agora para todos os destinatários?`)) return;
  try {
    await apiFetch('POST', `/v1/reports/${id}/send?tenant=` + encodeURIComponent(state.tenant));
    toast(`Relatório "${name}" enviado! Verifique as caixas de entrada em alguns instantes.`);
  } catch (e) {
    toast('Erro ao enviar: ' + e.message);
  }
}

/* ============================================================
   PRÉ-VISUALIZAÇÃO (mesmo PDF que iria por e-mail, sem enviar)
   ============================================================ */
async function previewReport(id, name, btn) {
  const old = btn ? btn.innerHTML : null;
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Gerando…'; }
  try {
    const r = await fetch(
      state.url + `/v1/reports/${id}/preview?tenant=` + encodeURIComponent(state.tenant),
      { headers: _authHeaders(), credentials: 'include' }
    );
    if (r.status === 401) { if (typeof _handleAuthFail === 'function') _handleAuthFail(); throw new Error('sessão expirada'); }
    if (!r.ok) { const t = await r.text().catch(() => r.statusText); throw new Error(t); }
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const win  = window.open(url, '_blank');
    if (!win) {  // popup bloqueado → baixa o arquivo
      const a = document.createElement('a');
      a.href = url; a.download = `workpulse_${(name || 'relatorio').toLowerCase().replace(/\s+/g, '_')}.pdf`;
      document.body.appendChild(a); a.click(); a.remove();
    }
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (e) {
    toast('Erro ao gerar a pré-visualização: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}

/* ============================================================
   HISTÓRICO DE ENVIOS
   ============================================================ */
async function showReportHistory(id, name) {
  document.getElementById('reportHistoryModal')?.remove();

  let rows = [];
  try {
    const data = await api(`/v1/reports/${id}/history?tenant=` + encodeURIComponent(state.tenant));
    rows = data.history || [];
  } catch (e) {
    toast('Erro ao carregar histórico: ' + e.message);
    return;
  }

  const rowsHtml = rows.length
    ? rows.map(r => {
        const recipients = (typeof r.recipients === 'string' ? JSON.parse(r.recipients) : r.recipients) || [];
        const badge = r.status === 'ok'
          ? '<span class="badge badge-ok">OK</span>'
          : '<span class="badge badge-error">Erro</span>';
        const sentAt = r.sent_at ? new Date(r.sent_at).toLocaleString('pt-BR') : '—';
        return `<tr>
          <td>${sentAt}</td>
          <td>${badge}</td>
          <td>${recipients.join(', ')}</td>
          <td style="color:var(--danger);font-size:11px">${r.error_msg || ''}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="4" class="empty-state">Nenhum envio registrado ainda.</td></tr>';

  const modal = document.createElement('div');
  modal.id = 'reportHistoryModal';
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal-box" style="max-width:680px">
      <h3 class="modal-title">📋 Histórico — ${_esc(name)}</h3>
      <div style="overflow-x:auto">
        <table class="table" style="font-size:13px">
          <thead><tr>
            <th>Enviado em</th><th>Status</th><th>Destinatários</th><th>Erro</th>
          </tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
      <div class="modal-actions">
        <button class="btn btn-outline" onclick="document.getElementById('reportHistoryModal').remove()">Fechar</button>
      </div>
    </div>`;

  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

/* ============================================================
   UTILIDADES
   ============================================================ */
function _esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}


/* ============================================================
   CONTA DE E-MAIL (SMTP do cliente) — relatórios e alertas
   ============================================================ */
const EM_PRESETS = {
  gmail:   { host: 'smtp.gmail.com',        port: 587, security: 'tls' },
  outlook: { host: 'smtp.office365.com',    port: 587, security: 'tls' },
};

function emProviderPreset() {
  const p = document.getElementById('emProvider').value;
  const hint = document.getElementById('emGmailHint');
  if (hint) hint.style.display = (p === 'gmail') ? 'block' : 'none';
  const preset = EM_PRESETS[p];
  if (preset) {
    document.getElementById('emHost').value     = preset.host;
    document.getElementById('emPort').value     = preset.port;
    document.getElementById('emSecurity').value = preset.security;
  }
}

async function loadEmailConfig() {
  try {
    const d = await api('/v1/tenant-email-config?tenant=' + encodeURIComponent(state.tenant));
    document.getElementById('emProvider').value   = d.provider || 'gmail';
    document.getElementById('emHost').value       = d.host || '';
    document.getElementById('emPort').value       = d.port || 587;
    document.getElementById('emSecurity').value   = d.security || 'tls';
    document.getElementById('emUser').value       = d.username || '';
    document.getElementById('emFromName').value   = d.from_name || '';
    document.getElementById('emFromEmail').value  = d.from_email || '';
    document.getElementById('emEnabled').checked  = !!d.enabled;
    const pass = document.getElementById('emPass');
    pass.value = '';
    pass.placeholder = d.has_password ? '•••••••• (senha salva — deixe em branco p/ manter)' : 'senha ou senha de app';
    const hint = document.getElementById('emGmailHint');
    if (hint) hint.style.display = (d.provider === 'gmail' || !d.configured) ? 'block' : 'none';
    emRenderStatus(d);
  } catch (e) {
    emSetStatus('Erro ao carregar: ' + e.message, 'var(--danger)');
  }
}

function emRenderStatus(d) {
  if (!d || !d.configured) { emSetStatus('Ainda não configurada — relatórios não serão enviados.', 'var(--text-dim)'); return; }
  let txt = d.enabled ? '✅ Habilitada.' : '⚠️ Configurada, mas desabilitada (não envia).';
  if (d.last_test_at) {
    const when = new Date(d.last_test_at).toLocaleString('pt-BR');
    txt += d.last_test_ok ? `  Último teste OK em ${when}.` : `  Último teste FALHOU em ${when}: ${d.last_test_error || ''}`;
  }
  emSetStatus(txt, d.enabled ? '#4ade80' : '#facc15');
}

function emSetStatus(msg, color) {
  const el = document.getElementById('emStatus');
  if (el) { el.textContent = msg; el.style.color = color || 'var(--text-dim)'; }
}

function _emCollect() {
  return {
    provider:   document.getElementById('emProvider').value,
    host:       document.getElementById('emHost').value.trim(),
    port:       parseInt(document.getElementById('emPort').value, 10) || 587,
    security:   document.getElementById('emSecurity').value,
    username:   document.getElementById('emUser').value.trim(),
    password:   document.getElementById('emPass').value,            // vazio => mantém
    from_name:  document.getElementById('emFromName').value.trim(),
    from_email: document.getElementById('emFromEmail').value.trim() || document.getElementById('emUser').value.trim(),
    enabled:    document.getElementById('emEnabled').checked,
  };
}

async function saveEmailConfig() {
  const body = _emCollect();
  if (!body.host || !body.username) { emSetStatus('Preencha servidor e usuário.', 'var(--danger)'); return; }
  try {
    await apiFetch('PUT', '/v1/tenant-email-config?tenant=' + encodeURIComponent(state.tenant), body);
    toast('Conta de e-mail salva.');
    loadEmailConfig();
  } catch (e) { emSetStatus('Falha ao salvar: ' + e.message, 'var(--danger)'); }
}

async function testEmailConfig() {
  emSetStatus('Enviando e-mail de teste…', 'var(--text-dim)');
  try {
    // salva o que estiver no formulário antes de testar, para refletir mudanças
    await apiFetch('PUT', '/v1/tenant-email-config?tenant=' + encodeURIComponent(state.tenant), _emCollect());
    const r = await apiFetch('POST', '/v1/tenant-email-config/test?tenant=' + encodeURIComponent(state.tenant), {});
    emSetStatus('✅ Teste enviado para ' + (r && r.sent_to ? r.sent_to : 'sua conta') + '.', '#4ade80');
    loadEmailConfig();
  } catch (e) { emSetStatus('❌ Falha no teste: ' + e.message, 'var(--danger)'); }
}
