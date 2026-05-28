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
    : ['coletas'];

  const sectionChecks = Object.entries(SECTION_LABELS).map(([key, label]) => `
    <label class="checkbox-label">
      <input type="checkbox" name="section_${key}" value="${key}"
        ${sections.includes(key) ? 'checked' : ''}> ${label}
    </label>`).join('');

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
        <label>Seções incluídas no relatório</label>
        <div class="checkbox-group">${sectionChecks}</div>
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
  const sections = ['coletas','jornada','tendencias','alertas']
    .filter(k => document.querySelector(`input[name="section_${k}"]`)?.checked);

  if (!name) { toast('Informe o nome do relatório.'); return null; }
  if (!recipients.length) { toast('Informe ao menos um e-mail válido.'); return null; }
  if (!sections.length) { toast('Selecione ao menos uma seção.'); return null; }

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
