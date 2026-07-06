// =====================================================================
// wp-departments.js — Aba "Departamentos" (visualização de carga)
// =====================================================================

async function loadDepartments() {
  try {
    const { since, until } = wpDP.getRange('rangeDays');
    const data  = state.lastOverview ||
      await api(`/v1/workforce/overview?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`);
    const depts = data.by_department || [];
    const ct    = chartTheme();

    if (state.charts.dept) state.charts.dept.destroy();
    state.charts.dept = new Chart(document.getElementById('chartDept').getContext('2d'), {
      type: 'bar',
      data: {
        labels: depts.map(d => d.dept),
        datasets: [{
          label: 'Horas ativas',
          data: depts.map(d => +(d.active / 3600).toFixed(1)),
          backgroundColor: ['#6c8cff','#34d399','#f59e0b','#a78bfa','#22d3ee','#f472b6','#fb7185','#fbbf24'],
          borderRadius: 6
        }]
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: ct.label }, grid: { color: ct.grid } },
          y: { ticks: { color: ct.legend, font: { size: 13 } }, grid: { display: false } }
        }
      }
    });

    const total      = depts.reduce((a, d) => a + (d.active || 0), 0) || 1;
    const totalUsers = depts.reduce((a, d) => a + (d.users || 0), 0);

    document.getElementById('deptSummary').innerHTML = `
      <div style="display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border-soft)">
        <span style="color:var(--text-dim);font-size:13px">Departamentos com atividade</span>
        <strong>${depts.length}</strong>
      </div>
      <div style="display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border-soft)">
        <span style="color:var(--text-dim);font-size:13px">Total de pessoas</span>
        <strong>${totalUsers}</strong>
      </div>
      <div style="display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border-soft)">
        <span style="color:var(--text-dim);font-size:13px">Horas totais (7d)</span>
        <strong>${fmtH(total)}h</strong>
      </div>
      <div style="display:flex;justify-content:space-between;padding:10px 0">
        <span style="color:var(--text-dim);font-size:13px">Média por pessoa</span>
        <strong>${fmtH(total / (totalUsers || 1))}h</strong>
      </div>
    `;
  } catch (e) { toast('Erro: ' + e.message); }
}
