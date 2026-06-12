// =====================================================================
// wp-locations.js — Coletas → Localização (IPs públicos de conexão + geo)
// =====================================================================

let _locInit = false;
let _locData = null;

async function loadLocations() {
  if (!_locInit) {
    wpDP.init('locRange', { defaultDays: 7, onChange: loadLocations });
    _locInit = true;
  }
  const tb = document.querySelector('#locTable tbody');
  try {
    const { since, until } = wpDP.getRange('locRange');
    const d = await api(`/v1/locations?tenant=${encodeURIComponent(state.tenant)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`);
    _locData = d.users || [];
    locRenderTable();
    const mv = document.getElementById('locMapView');
    if (mv && !mv.classList.contains('hide')) _locRenderMap();
  } catch (e) {
    if (tb) tb.innerHTML = `<tr><td colspan="6" class="empty-state">Erro: ${escHtml(e.message)}</td></tr>`;
  }
}

function locRenderTable() {
  const tb = document.querySelector('#locTable tbody');
  if (!tb) return;
  const q = (document.getElementById('locFilter').value || '').toLowerCase();
  const rows = (_locData || []).filter(u =>
    !q || (u.display_name || '').toLowerCase().includes(q)
       || (u.last_geo || '').toLowerCase().includes(q)
       || (u.last_ip || '').includes(q));
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty-state">Sem dados de IP no período. Os IPs começam a ser registrados após o deploy desta versão.</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(u => {
    const changed = u.distinct_ips > 1;
    const badge = changed
      ? `<span class="pill" style="background:rgba(245,158,11,.15);color:#f59e0b;border-color:rgba(245,158,11,.4)">${u.distinct_ips} IPs</span>`
      : `<span style="color:var(--text-dim)">${u.distinct_ips}</span>`;
    const uid = (u.user_id || '').replace(/'/g, "\\'");
    const nm  = (u.display_name || '').replace(/'/g, "\\'");
    return `<tr>
      <td>${avatar(u.display_name)} <strong>${escHtml(u.display_name)}</strong></td>
      <td class="num">${badge}</td>
      <td><code style="font-size:12px;color:var(--text-dim)">${escHtml(u.last_ip || '—')}</code></td>
      <td>${escHtml(u.last_geo || '—')}</td>
      <td>${u.last_seen ? relTime(u.last_seen) : '—'}</td>
      <td><button class="ghost" onclick="locDrill('${uid}','${nm}')">detalhes</button></td>
    </tr>`;
  }).join('');
}

async function locDrill(uid, name) {
  try {
    const { since, until } = wpDP.getRange('locRange');
    const d = await api(`/v1/locations?tenant=${encodeURIComponent(state.tenant)}&user_id=${encodeURIComponent(uid)}&since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`);
    const el = document.getElementById('locDrill');
    if (!el) return;
    el.classList.remove('hide');
    const items = d.items || [];
    const rowsHtml = items.length ? items.map(it => `<tr>
      <td>${escHtml(it.day)}</td>
      <td><code style="font-size:12px;color:var(--text-dim)">${escHtml(it.ip)}</code></td>
      <td>${escHtml(it.geo || '—')}</td>
      <td>${escHtml(it.org || '—')}</td>
      <td>${_locHM(it.first_seen)}–${_locHM(it.last_seen)}</td>
      <td class="num">${it.hits}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="empty-state">Sem registros no período.</td></tr>';
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <h4 style="margin:0">${escHtml(name)}
          <small style="color:var(--text-muted);font-weight:400">· ${d.distinct_ips} IP(s) distinto(s) no período</small></h4>
        <button class="ghost" title="Fechar"
          onclick="document.getElementById('locDrill').classList.add('hide')"
          style="font-size:20px;line-height:1;padding:2px 10px;opacity:.6">×</button>
      </div>
      <table style="margin-top:0">
        <thead><tr><th>Dia</th><th>IP</th><th>Localização</th><th>Operadora</th><th>Horário (1º–último)</th><th class="num">Acessos</th></tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>`;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { toast('Erro: ' + e.message); }
}

function _locHM(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
}


// ── Mapa (Leaflet) ────────────────────────────────────────────────────
let _locMap = null, _locMarkers = null, _locTile = null, _locTileStyle = null;

function locShowList() {
  document.getElementById('locMapView').classList.add('hide');
  document.getElementById('locListView').classList.remove('hide');
  document.getElementById('locBtnMap').className = 'ghost';
  document.getElementById('locBtnList').className = 'primary';
}

function locShowMap() {
  document.getElementById('locListView').classList.add('hide');
  document.getElementById('locMapView').classList.remove('hide');
  document.getElementById('locBtnList').className = 'ghost';
  document.getElementById('locBtnMap').className = 'primary';
  _locRenderMap();
}

// Mapa segue o tema: claro = Voyager (limpo/colorido), escuro = dark matter.
function _locApplyTiles() {
  if (!_locMap || typeof L === 'undefined') return;
  const light = document.body.classList.contains('theme-light');
  const style = light ? 'voyager' : 'dark';
  if (style === _locTileStyle && _locTile) return;
  const url = light
    ? 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png'
    : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  if (_locTile) _locMap.removeLayer(_locTile);
  _locTile = L.tileLayer(url, {
    attribution: '© OpenStreetMap · © CARTO', subdomains: 'abcd', maxZoom: 19,
  }).addTo(_locMap);
  _locTile.bringToBack();
  _locTileStyle = style;
}

function _locInitMap() {
  if (_locMap || typeof L === 'undefined') return;
  _locMap = L.map('locMap', { zoomControl: true, scrollWheelZoom: true })
    .setView([-14.2, -51.9], 4);   // centro aproximado do Brasil
  _locMarkers = L.layerGroup().addTo(_locMap);
  _locApplyTiles();
}

function _locRenderMap() {
  if (typeof L === 'undefined') return;
  _locInitMap();
  _locApplyTiles();
  setTimeout(() => { if (_locMap) _locMap.invalidateSize(); }, 60);
  if (!_locMarkers) return;
  _locMarkers.clearLayers();

  const groups = {};
  (_locData || []).forEach(u => {
    if (u.last_lat == null || u.last_lon == null) return;
    const key = (+u.last_lat).toFixed(2) + ',' + (+u.last_lon).toFixed(2);
    if (!groups[key]) groups[key] = { lat: +u.last_lat, lon: +u.last_lon, city: u.last_city || u.last_geo || '—', people: [] };
    groups[key].people.push(u);
  });
  const pts = Object.values(groups);

  const emptyEl = document.getElementById('locMapEmpty');
  if (emptyEl) {
    emptyEl.style.display = pts.length ? 'none' : 'block';
    if (!pts.length) emptyEl.textContent = 'Ainda sem coordenadas para o mapa — a geolocalização precisa estar resolvendo os IPs.';
  }

  const bounds = [];
  pts.forEach(g => {
    const n = g.people.length;
    const recent = g.people.some(p => p.last_seen && (Date.now() - new Date(p.last_seen).getTime()) < 30 * 60000);
    const color = recent ? '#4ade80' : '#6c8cff';
    const r = Math.min(24, 7 + n * 3);
    // halo (brilho) + ponto
    L.circleMarker([g.lat, g.lon], { radius: r + 8, weight: 0, fillColor: color, fillOpacity: 0.12, interactive: false }).addTo(_locMarkers);
    const m = L.circleMarker([g.lat, g.lon], { radius: r, color: '#ffffff', weight: 2, fillColor: color, fillOpacity: 0.85 }).addTo(_locMarkers);
    const names = g.people.map(p => '• ' + escHtml(p.display_name)).join('<br>');
    m.bindTooltip(`${escHtml(g.city)} — ${n} pessoa(s)`, { direction: 'top' });
    m.bindPopup(`<strong>${escHtml(g.city)}</strong><br><span style="font-size:12px">${names}</span>`);
    bounds.push([g.lat, g.lon]);
  });
  if (bounds.length) _locMap.fitBounds(bounds, { padding: [50, 50], maxZoom: 7 });
}
