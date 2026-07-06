// =============================================================================
// wp-datepicker.js — WP Date Picker
// =============================================================================
// Reusable date picker with preset shortcuts and calendar range selection.
//
// Usage:
//   wpDP.init('myPickerId', { defaultDays: 7, onChange: myCallback });
//   const { since, until } = wpDP.getRange('myPickerId');  // ISO strings
//
// HTML trigger element just needs an id:
//   <div id="myPickerId"></div>
// =============================================================================

const wpDP = (() => {
  'use strict';

  /* ── Constants ───────────────────────────────────────────────────── */
  const PRESETS = [
    { days: 1,  label: 'Hoje' },
    { days: 7,  label: 'Últimos 7 dias' },
    { days: 30, label: 'Últimos 30 dias' },
    { days: 90, label: 'Últimos 90 dias' },
  ];

  const WEEKDAYS = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'];

  const MONTHS = [
    'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
    'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro',
  ];

  /* ── State ───────────────────────────────────────────────────────── */
  // _inst[id] = { mode, days, since: Date|null, until: Date|null, onChange, label }
  const _inst  = {};
  // _draft[id] = working copy while panel is open
  const _draft = {};

  /* ── Date utils ──────────────────────────────────────────────────── */
  function _today() {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  }

  function _eod(d) {
    const e = new Date(d);
    e.setHours(23, 59, 59, 999);
    return e;
  }

  function _sinceForDays(days) {
    if (days === 1) return _today();
    // "last N days" includes today: go back (N-1) full days from today's midnight
    const d = _today();
    d.setDate(d.getDate() - (days - 1));
    return d;
  }

  function _isoDate(d) {
    const y  = d.getFullYear();
    const m  = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${dd}`;
  }

  function _sameDay(a, b) {
    return a && b && _isoDate(a) === _isoDate(b);
  }

  function _fmtShort(d) {
    return d.toLocaleDateString('pt-BR', { day: 'numeric', month: 'short' });
  }

  function _fmtRange(since, until) {
    const sy = since.getFullYear();
    const uy = until.getFullYear();
    if (sy === uy) {
      return `${_fmtShort(since)} – ${_fmtShort(until)} ${uy}`;
    }
    return `${since.toLocaleDateString('pt-BR', { day: 'numeric', month: 'short', year: 'numeric' })} – ${until.toLocaleDateString('pt-BR', { day: 'numeric', month: 'short', year: 'numeric' })}`;
  }

  /* ── Public API ──────────────────────────────────────────────────── */

  /**
   * Returns { since: ISOstring, until: ISOstring } for the picker.
   */
  function getRange(id) {
    const s = _inst[id];
    if (!s) {
      return { since: _sinceForDays(7).toISOString(), until: _eod(_today()).toISOString() };
    }
    if (s.mode === 'preset') {
      return { since: _sinceForDays(s.days).toISOString(), until: _eod(_today()).toISOString() };
    }
    return {
      since: s.since.toISOString(),
      until: _eod(s.until).toISOString(),
    };
  }

  /**
   * Returns the number of days in the current selection.
   * Useful for code that iterates day-by-day.
   */
  function getDays(id) {
    const s = _inst[id];
    if (!s || s.mode === 'preset') return s ? s.days : 7;
    const ms = _eod(s.until) - s.since;
    return Math.max(1, Math.ceil(ms / 86400000));
  }

  /**
   * Initialize picker on element #id.
   * opts: { defaultDays, onChange }
   */
  function init(id, opts = {}) {
    const el = document.getElementById(id);
    if (!el) return;

    const days = opts.defaultDays || 7;
    _inst[id] = {
      mode:     'preset',
      days,
      since:    null,
      until:    null,
      onChange: opts.onChange || null,
      label:    _labelForPreset(days),
    };

    el.classList.add('wp-dp-trigger');
    el.setAttribute('tabindex', '0');
    _renderTrigger(id);

    el.addEventListener('click', e => { e.stopPropagation(); _toggle(id); });
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _toggle(id); }
    });
  }

  /* ── Trigger ─────────────────────────────────────────────────────── */

  function _labelForPreset(days) {
    return PRESETS.find(p => p.days === days)?.label || `Últimos ${days} dias`;
  }

  function _renderTrigger(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = `<span class="wp-dp-lbl">${_inst[id].label}</span><span class="wp-dp-caret">▾</span>`;
  }

  /* ── Panel open/close ────────────────────────────────────────────── */

  function _toggle(id) {
    const existing = document.querySelector(`.wp-dp-panel[data-for="${id}"]`);
    if (existing) { _closePanel(id); return; }
    document.querySelectorAll('.wp-dp-panel').forEach(p => _closePanel(p.dataset.for));
    _openPanel(id);
  }

  function _closePanel(id) {
    const p = document.querySelector(`.wp-dp-panel[data-for="${id}"]`);
    if (p) p.remove();
    delete _draft[id];
    document.getElementById(id)?.classList.remove('wp-dp-open');
  }

  function _openPanel(id) {
    const s  = _inst[id];
    const el = document.getElementById(id);
    if (!el) return;

    const today = _today();
    _draft[id] = {
      mode:      s.mode,
      days:      s.days,
      since:     s.since ? new Date(s.since) : null,
      until:     s.until ? new Date(s.until) : null,
      viewYear:  today.getFullYear(),
      viewMonth: today.getMonth(),
      picking:   null,
    };

    const panel = document.createElement('div');
    panel.className = 'wp-dp-panel';
    panel.dataset.for = id;
    document.body.appendChild(panel);
    panel.addEventListener('click', e => e.stopPropagation());

    el.classList.add('wp-dp-open');
    _paintPanel(id);
    requestAnimationFrame(() => _positionPanel(id));
  }

  function _positionPanel(id) {
    const panel   = document.querySelector(`.wp-dp-panel[data-for="${id}"]`);
    const trigger = document.getElementById(id);
    if (!panel || !trigger) return;

    const rect = trigger.getBoundingClientRect();
    const pw   = panel.offsetWidth  || 470;
    const ph   = panel.offsetHeight || 330;

    let left = rect.left;
    if (left + pw > window.innerWidth - 8)  left = window.innerWidth - pw - 8;
    if (left < 8) left = 8;

    let top = rect.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = rect.top - ph - 6;

    panel.style.position = 'fixed';
    panel.style.top  = top  + 'px';
    panel.style.left = left + 'px';
  }

  /* ── Panel rendering ─────────────────────────────────────────────── */

  function _paintPanel(id) {
    const panel = document.querySelector(`.wp-dp-panel[data-for="${id}"]`);
    if (!panel) return;
    const d = _draft[id];

    /* ----- presets sidebar ----- */
    const presetsHtml = PRESETS.map(p => {
      const active = d.mode === 'preset' && d.days === p.days;
      return `<button class="wp-dp-ps${active ? ' wp-dp-ps-on' : ''}"
                onclick="wpDP._ps('${id}',${p.days})">${p.label}</button>`;
    }).join('') + `<button class="wp-dp-ps wp-dp-ps-custom${d.mode === 'range' ? ' wp-dp-ps-on' : ''}"
        onclick="wpDP._psRange('${id}')">Personalizado</button>`;

    /* ----- calendar header ----- */
    const today = _today();
    const atMax = (d.viewYear === today.getFullYear() && d.viewMonth === today.getMonth());

    const calHeader = `
      <div class="wp-dp-cal-nav">
        <button class="wp-dp-nav" onclick="wpDP._prevM('${id}')">‹</button>
        <span class="wp-dp-month">${MONTHS[d.viewMonth]} ${d.viewYear}</span>
        <button class="wp-dp-nav${atMax ? ' wp-dp-nav-dis' : ''}" onclick="wpDP._nextM('${id}')" ${atMax ? 'disabled' : ''}>›</button>
      </div>
      <div class="wp-dp-wdays">${WEEKDAYS.map(w => `<span>${w}</span>`).join('')}</div>
    `;

    /* ----- day cells ----- */
    const cells = _buildCells(id);

    /* ----- range hint footer ----- */
    let hint = '';
    if (d.mode === 'range') {
      if (!d.since) {
        hint = 'Clique para selecionar o início do período';
      } else if (!d.until) {
        hint = `Início: ${d.since.toLocaleDateString('pt-BR')} — agora clique o fim`;
      } else {
        hint = _fmtRange(d.since, d.until);
      }
    }

    panel.innerHTML = `
      <div class="wp-dp-body">
        <div class="wp-dp-sidebar">${presetsHtml}</div>
        <div class="wp-dp-cal">
          ${calHeader}
          <div class="wp-dp-grid">${cells}</div>
          ${hint ? `<div class="wp-dp-hint">${hint}</div>` : ''}
        </div>
      </div>
      <div class="wp-dp-foot">
        <button class="wp-dp-cancel" onclick="wpDP._cancel('${id}')">Cancelar</button>
        <button class="wp-dp-apply" onclick="wpDP._apply('${id}')">Aplicar</button>
      </div>`;
  }

  function _buildCells(id) {
    const d     = _draft[id];
    const year  = d.viewYear;
    const month = d.viewMonth;
    const today = _today();

    const firstDow   = new Date(year, month, 1).getDay();
    const daysInMon  = new Date(year, month + 1, 0).getDate();

    // Selected range to highlight
    let selA = null, selB = null;
    if (d.mode === 'preset') {
      selA = _sinceForDays(d.days);
      selB = _eod(today);
    } else {
      selA = d.since;
      selB = d.until ? _eod(d.until) : null;
    }

    let out = '';

    // Leading blank cells
    for (let i = 0; i < firstDow; i++) out += '<span class="wp-dp-cell wp-dp-blank"></span>';

    for (let day = 1; day <= daysInMon; day++) {
      const dt       = new Date(year, month, day);
      const isFuture = dt > today;
      const isToday  = _sameDay(dt, today);

      let cls = 'wp-dp-cell';
      if (isFuture) cls += ' wp-dp-future';
      if (isToday)  cls += ' wp-dp-today';

      if (!isFuture && selA) {
        const isStart  = _sameDay(dt, selA);
        const isEnd    = selB && _sameDay(dt, selB);
        const inRange  = selB && dt > selA && dt < selB;

        if (isStart && (!selB || _sameDay(selA, selB))) cls += ' wp-dp-sel wp-dp-sel-single';
        else if (isStart) cls += ' wp-dp-sel wp-dp-sel-start';
        else if (isEnd)   cls += ' wp-dp-sel wp-dp-sel-end';
        else if (inRange) cls += ' wp-dp-in-range';
      }

      const click = isFuture ? '' : `onclick="wpDP._day('${id}',${year},${month},${day})"`;
      out += `<span class="${cls}" ${click}>${day}</span>`;
    }

    return out;
  }

  /* ── Interactions ────────────────────────────────────────────────── */

  function _ps(id, days) {
    const d   = _draft[id];
    d.mode    = 'preset';
    d.days    = days;
    d.since   = null;
    d.until   = null;
    d.picking = null;
    _paintPanel(id);
  }

  function _psRange(id) {
    const d   = _draft[id];
    d.mode    = 'range';
    d.since   = null;
    d.until   = null;
    d.picking = 'start';
    _paintPanel(id);
  }

  function _day(id, year, month, day) {
    const d       = _draft[id];
    const clicked = new Date(year, month, day);

    if (d.mode === 'preset') {
      // Switch to custom range
      d.mode  = 'range';
      d.since = clicked;
      d.until = null;
    } else {
      if (!d.since || d.picking === 'start') {
        d.since = clicked;
        d.until = null;
      } else if (d.until !== null) {
        // Both already set — restart selection
        d.since = clicked;
        d.until = null;
      } else {
        if (clicked < d.since) {
          // Clicked before start — restart
          d.since = clicked;
          d.until = null;
        } else {
          d.until   = clicked;
          d.picking = null;
        }
      }
    }

    // After first click, switch to 'end' picking
    if (d.since && !d.until) d.picking = 'end';

    _paintPanel(id);
  }

  function _prevM(id) {
    const d = _draft[id];
    if (d.viewMonth === 0) { d.viewMonth = 11; d.viewYear--; }
    else d.viewMonth--;
    _paintPanel(id);
  }

  function _nextM(id) {
    const d     = _draft[id];
    const today = _today();
    if (d.viewYear === today.getFullYear() && d.viewMonth === today.getMonth()) return;
    if (d.viewMonth === 11) { d.viewMonth = 0; d.viewYear++; }
    else d.viewMonth++;
    _paintPanel(id);
  }

  function _cancel(id) {
    _closePanel(id);
  }

  function _apply(id) {
    const d = _draft[id];
    const s = _inst[id];

    if (d.mode === 'preset') {
      s.mode  = 'preset';
      s.days  = d.days;
      s.since = null;
      s.until = null;
      s.label = _labelForPreset(d.days);
    } else if (d.since) {
      // Single click = single day range
      const until = d.until || d.since;
      s.mode  = 'range';
      s.since = d.since;
      s.until = until;
      s.label = _sameDay(d.since, until)
        ? d.since.toLocaleDateString('pt-BR', { day: 'numeric', month: 'short', year: 'numeric' })
        : _fmtRange(d.since, until);
    } else {
      _closePanel(id);
      return;
    }

    _closePanel(id);
    _renderTrigger(id);
    if (s.onChange) s.onChange();
  }

  /* ── Global close on outside click / ESC ────────────────────────── */
  document.addEventListener('click', () => {
    document.querySelectorAll('.wp-dp-panel').forEach(p => _closePanel(p.dataset.for));
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape')
      document.querySelectorAll('.wp-dp-panel').forEach(p => _closePanel(p.dataset.for));
  });

  /* ── Expose ──────────────────────────────────────────────────────── */
  return { init, getRange, getDays, _ps, _psRange, _day, _prevM, _nextM, _cancel, _apply };
})();
