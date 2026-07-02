// ── Soundboard tab (Sessions B+C) ─────────────────────────────────────────────
// Data-driven quip + clip manager. Reads /api/soundboard, edits a live in-memory
// document, POSTs the whole document to /api/soundboard/save (which writes RAM+SD
// and tells the assistant to reload clips + re-synthesize changed quips). All clip
// text is rendered with textContent / value — never innerHTML — so arbitrary clip
// names and quip lines can never inject markup.

let _sbData = null;                                  // {version, clips:[...], quips:{...}}
let _sbMeta = { emotions: [], gestureKeys: [], diskClips: [] };
let _sbDirty = false;

function _sbMarkDirty() {
  _sbDirty = true;
  const b = document.getElementById('sb-save-btn');
  if (b) { b.classList.add('btn-primary'); b.textContent = 'Save All Changes *'; }
}

async function fetchSoundboard() {
  const host = document.getElementById('sb-body');
  if (!host) return;
  host.textContent = 'Loading...';
  let j;
  try { j = await (await fetch('/api/soundboard')).json(); }
  catch (e) { host.textContent = 'Error loading soundboard.'; return; }
  if (!j.ok) { host.textContent = 'Error: ' + (j.error || 'unknown'); return; }
  _sbData = { version: j.version, clips: j.clips || [], quips: j.quips || {} };
  _sbMeta = {
    emotions:    j.valid_emotions || [],
    gestureKeys: j.gesture_keys || [],
    diskClips:   j.disk_clips || [],
  };
  _sbDirty = false;
  renderSoundboard();
}

function renderSoundboard() {
  const host = document.getElementById('sb-body');
  host.textContent = '';
  if (_sbSub === 'clips') host.appendChild(_sbBuildClips());
  else host.appendChild(_sbBuildQuips());
  const sv = document.getElementById('sb-save-btn');
  if (sv) { sv.classList.toggle('btn-primary', _sbDirty); sv.textContent = _sbDirty ? 'Save All Changes *' : 'Save All Changes'; }
}

let _sbSub = 'clips';
function sbSubTab(which) {
  _sbSub = which;
  document.querySelectorAll('.sb-subtab').forEach(b => b.classList.toggle('active', b.dataset.sub === which));
  renderSoundboard();
}

// ── small DOM helpers ─────────────────────────────────────────────────────────
function _el(tag, props, kids) {
  const e = document.createElement(tag);
  if (props) for (const k in props) {
    if (k === 'style') e.setAttribute('style', props[k]);
    else if (k === 'class') e.className = props[k];
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), props[k]);
    else if (k === 'text') e.textContent = props[k];
    else e[k] = props[k];
  }
  (kids || []).forEach(c => e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
  return e;
}

// Hour names — mirror of assistant._HOUR_NAMES (the {hour} source for top-of-hour).
const _SB_HOUR_NAMES = ['Midnight','One','Two','Three','Four','Five','Six','Seven',
  'Eight','Nine','Ten','Eleven','Noon','One','Two','Three','Four','Five','Six',
  'Seven','Eight','Nine','Ten','Eleven'];

// ── Group mapping for voice-clone + Bluey clips ───────────────────────────────────
const _SB_BLUEY = new Set(['Bonjour.wav','BonjourBonjooour.wav','Discotech.wav',
                            'HomeSweetLonelyHome.wav','MagicClaw.wav','wheremyPassport.wav']);
function _sbClipGroup(file) {
  if (_SB_BLUEY.has(file)) return 'Bluey — Bandit';
  const m = file.match(/^iris_clip_(\d+)\.wav$/i);
  if (!m) return 'Custom';
  const n = parseInt(m[1], 10);
  if (n <=  5) return 'Acknowledgments / Greetings';
  if (n <= 10) return 'Dismissals / Snark';
  if (n <= 15) return 'Confusion / Curiosity';
  if (n <= 20) return 'Annoyance / Anger';
  if (n <= 25) return 'Rare Warmth';
  if (n <= 30) return 'Situational';
  if (n <= 35) return 'Reactions';
  if (n <= 40) return 'Personality';
  if (n <= 45) return 'Error / Unknown';
  if (n <= 50) return 'Sleep / Wake';
  if (n <= 80) return 'Additional Snark';
  return 'Custom';
}

// ── CLIPS ─────────────────────────────────────────────────────────────────────
function _sbBuildClips() {
  const wrap = _el('div');

  // Test utterance box
  const testDiv = _el('div', { style: 'display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap;padding:8px;background:rgba(100,100,255,0.05);border:1px solid var(--border);border-radius:6px' });
  const testInput = _el('input', {
    id: 'sb-test-utter', type: 'text', placeholder: 'Test utterance (Enter to test)...',
    style: 'flex:1;min-width:180px;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:4px 8px;font-size:12px',
    onkeydown: (ev) => { if (ev.key === 'Enter') _sbTestUtter(); },
  });
  const testEmoSel = _el('select', {
    id: 'sb-test-emo',
    style: 'background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:4px;font-size:12px',
  }, [_el('option', { value: '', text: 'any emotion' })]);
  _sbMeta.emotions.forEach(e => testEmoSel.appendChild(_el('option', { value: e, text: e })));
  const testBtn = _el('button', { class: 'btn btn-sm', text: 'Test clip match',
    style: 'background:var(--input-bg);border:1px solid var(--border);color:var(--text)',
    onclick: _sbTestUtter });
  const testResult = _el('span', { id: 'sb-test-result', style: 'font-size:12px;color:var(--muted)' });
  testDiv.append(testInput, testEmoSel, testBtn, testResult);
  wrap.appendChild(testDiv);

  // Filter bar
  const bar = _el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px' });
  const search = _el('input', {
    id: 'sb-clip-search', type: 'text', placeholder: 'Filter by name / text / trigger...',
    style: 'flex:1;min-width:160px;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:4px 8px',
    oninput: () => _sbRenderClipRows(),
  });
  const emoSel = _el('select', {
    id: 'sb-clip-emo',
    style: 'background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:4px',
    onchange: () => _sbRenderClipRows(),
  }, [_el('option', { value: '', text: 'Any affect' })]);
  _sbMeta.emotions.forEach(e => emoSel.appendChild(_el('option', { value: e, text: e })));
  const onlyOn = _el('label', { style: 'font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px' }, [
    _el('input', { id: 'sb-clip-onlyon', type: 'checkbox', onchange: () => _sbRenderClipRows() }), 'enabled only',
  ]);
  const count = _el('span', { id: 'sb-clip-count', style: 'font-size:12px;color:var(--muted)' });
  bar.append(search, emoSel, onlyOn, count);
  wrap.appendChild(bar);

  // Upload
  const up = _el('div', { style: 'display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap' }, [
    _el('input', { id: 'sb-clip-file', type: 'file', accept: '.wav',
      style: 'color:var(--text);background:var(--input-bg);border:1px solid var(--border);border-radius:4px;padding:3px 6px' }),
    _el('button', { class: 'btn btn-sm', text: 'Upload WAV', onclick: _sbUploadClip,
      style: 'background:var(--input-bg);border:1px solid var(--border);color:var(--text)' }),
    _el('span', { id: 'sb-clip-upstatus', style: 'font-size:12px;color:var(--muted)' }),
  ]);
  wrap.appendChild(up);

  const list = _el('div', { id: 'sb-clip-list' });
  wrap.appendChild(list);
  setTimeout(_sbRenderClipRows, 0);
  return wrap;
}

function _sbRenderClipRows() {
  const list = document.getElementById('sb-clip-list');
  if (!list) return;
  const q = (document.getElementById('sb-clip-search').value || '').toLowerCase();
  const emo = document.getElementById('sb-clip-emo').value;
  const onlyOn = document.getElementById('sb-clip-onlyon').checked;
  list.textContent = '';
  let shown = 0;

  // Group clips in insertion order
  const groupOrder = [];
  const groupMap = {};
  _sbData.clips.forEach(c => {
    if (onlyOn && !c.enabled) return;
    if (emo && !(c.affect || []).includes(emo)) return;
    if (q) {
      const hay = (c.file + ' ' + (c.desc || '') + ' ' + (c.triggers || []).join(' ')).toLowerCase();
      if (!hay.includes(q)) return;
    }
    const g = _sbClipGroup(c.file);
    if (!groupMap[g]) { groupMap[g] = []; groupOrder.push(g); }
    groupMap[g].push(c);
  });

  if (groupOrder.length === 0) {
    list.appendChild(_el('div', { style: 'color:var(--muted);padding:8px', text: 'No clips match the filter.' }));
  } else {
    groupOrder.forEach(groupLabel => {
      const clips = groupMap[groupLabel];
      // Group header with enable-all toggle
      const ghdr = _el('div', { style: 'display:flex;align-items:center;gap:8px;margin:10px 0 4px;padding:4px 0;border-bottom:1px solid var(--border)' });
      ghdr.appendChild(_el('span', { style: 'font-size:12px;font-weight:600;color:var(--muted)', text: groupLabel }));
      const toggleAll = (function(grpClips) {
        return function() {
          const newState = !grpClips.every(c => c.enabled);
          grpClips.forEach(c => { c.enabled = newState; });
          _sbMarkDirty();
          _sbRenderClipRows();
        };
      })(clips);
      const allEnabled = clips.every(c => c.enabled);
      ghdr.appendChild(_el('button', { class: 'btn btn-sm',
        text: allEnabled ? 'Disable all' : 'Enable all',
        style: 'font-size:11px;padding:1px 8px;background:var(--input-bg);border:1px solid var(--border);color:var(--muted)',
        onclick: toggleAll }));
      list.appendChild(ghdr);
      clips.forEach(c => { list.appendChild(_sbClipRow(c)); shown++; });
    });
  }

  const cnt = document.getElementById('sb-clip-count');
  if (cnt) cnt.textContent = shown + ' / ' + _sbData.clips.length + ' clips';
}

function _sbClipRow(c) {
  const missing = !_sbMeta.diskClips.includes(c.file);
  const row = _el('div', {
    style: 'border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px;' +
           'background:' + (c.enabled ? 'rgba(80,200,120,0.06)' : 'transparent'),
  });
  // header line
  const head = _el('div', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap' });
  const tog = _el('input', { type: 'checkbox', checked: !!c.enabled, title: 'enable/disable',
    onchange: (ev) => { c.enabled = ev.target.checked; row.style.background = c.enabled ? 'rgba(80,200,120,0.06)' : 'transparent'; _sbMarkDirty(); } });
  const name = _el('span', { style: 'font-weight:600;font-size:13px;color:var(--text)', text: c.file });
  head.append(tog, name);
  if (missing) head.appendChild(_el('span', { style: 'font-size:11px;color:var(--red,#e05)', text: '⚠ WAV missing' }));
  const play = _el('button', { class: 'btn btn-sm', text: '▶', title: 'preview',
    style: 'margin-left:auto;background:var(--input-bg);border:1px solid var(--border);color:var(--text);padding:1px 8px',
    onclick: () => _sbPlayClip(c.file) });
  head.appendChild(play);
  row.appendChild(head);

  // Editable description
  const descWrap = _el('div', { style: 'margin:4px 0 2px' });
  descWrap.appendChild(_el('input', {
    type: 'text', value: c.desc || '', placeholder: 'description (shown in UI)',
    style: 'width:100%;box-sizing:border-box;background:var(--input-bg);border:1px solid var(--border);color:var(--muted);border-radius:4px;padding:2px 6px;font-size:12px;font-style:italic',
    onchange: (ev) => { c.desc = ev.target.value; _sbMarkDirty(); },
  }));
  row.appendChild(descWrap);

  // triggers + affect editors
  const grid = _el('div', { style: 'display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:start;margin-top:4px' });
  grid.appendChild(_el('label', { style: 'font-size:11px;color:var(--muted);padding-top:3px', text: 'triggers' }));
  grid.appendChild(_el('input', { type: 'text', value: (c.triggers || []).join(', '),
    placeholder: 'comma-separated keywords (blank = affect-only)',
    style: 'width:100%;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:3px 6px;font-size:12px',
    onchange: (ev) => { c.triggers = _sbSplitList(ev.target.value); _sbMarkDirty(); } }));
  grid.appendChild(_el('label', { style: 'font-size:11px;color:var(--muted);padding-top:3px', text: 'affect' }));
  grid.appendChild(_sbAffectChips(c));
  row.appendChild(grid);
  return row;
}

function _sbAffectChips(c) {
  const wrap = _el('div', { style: 'display:flex;gap:4px;flex-wrap:wrap;padding:2px 0' });
  _sbMeta.emotions.forEach(e => {
    const active = (c.affect || []).includes(e);
    const chip = _el('button', {
      class: 'btn btn-sm', text: e,
      style: 'padding:1px 7px;font-size:11px;border-radius:12px;transition:none;' +
             'background:' + (active ? 'var(--accent)' : 'var(--input-bg)') + ';' +
             'color:' + (active ? 'var(--bg,#000)' : 'var(--muted)') + ';' +
             'border:1px solid ' + (active ? 'var(--accent)' : 'var(--border)'),
    });
    chip.addEventListener('click', () => {
      const idx = (c.affect || []).indexOf(e);
      if (idx >= 0) {
        c.affect.splice(idx, 1);
        chip.style.background = 'var(--input-bg)';
        chip.style.color = 'var(--muted)';
        chip.style.borderColor = 'var(--border)';
      } else {
        if (!c.affect) c.affect = [];
        c.affect.push(e);
        chip.style.background = 'var(--accent)';
        chip.style.color = 'var(--bg,#000)';
        chip.style.borderColor = 'var(--accent)';
      }
      _sbMarkDirty();
    });
    wrap.appendChild(chip);
  });
  return wrap;
}

function _sbSplitList(s) {
  return (s || '').split(/[,\n]/).map(x => x.trim()).filter(Boolean);
}

async function _sbPlayClip(file) {
  try { await fetch('/api/clips/play/' + encodeURIComponent(file), { method: 'POST' }); }
  catch (e) { /* ignore */ }
}

async function _sbUploadClip() {
  const fi = document.getElementById('sb-clip-file');
  const st = document.getElementById('sb-clip-upstatus');
  if (!fi.files.length) { st.textContent = 'Choose a WAV first.'; return; }
  const fd = new FormData(); fd.append('file', fi.files[0]);
  st.textContent = 'Uploading...';
  try {
    const r = await fetch('/api/clips/upload', { method: 'POST', body: fd });
    const j = await r.json();
    if (j.ok) {
      st.textContent = 'Uploaded ' + j.filename;
      if (!_sbData.clips.some(c => c.file === j.filename)) {
        _sbData.clips.push({ file: j.filename, enabled: false, triggers: [], affect: [], desc: '' });
        _sbMarkDirty();
      }
      if (!_sbMeta.diskClips.includes(j.filename)) _sbMeta.diskClips.push(j.filename);
      _sbRenderClipRows();
    } else { st.textContent = 'Error: ' + (j.error || 'failed'); }
  } catch (e) { st.textContent = 'Upload failed.'; }
}

async function _sbTestUtter() {
  const u = (document.getElementById('sb-test-utter').value || '').trim();
  const e = document.getElementById('sb-test-emo').value;
  const st = document.getElementById('sb-test-result');
  if (!u) { st.textContent = 'Enter an utterance first.'; st.style.color = 'var(--muted)'; return; }
  st.textContent = 'Testing...'; st.style.color = 'var(--muted)';
  try {
    const url = '/api/soundboard/test?u=' + encodeURIComponent(u) + (e ? '&emotion=' + encodeURIComponent(e) : '');
    const j = await (await fetch(url)).json();
    if (!j.ok) { st.textContent = 'Error: ' + (j.error || '?'); st.style.color = 'var(--red,#e05)'; return; }
    if (!j.match) {
      st.textContent = 'No clip would fire.';
      st.style.color = 'var(--muted)';
    } else {
      st.textContent = 'Would fire: ' + j.match + (j.all_matches.length > 1 ? ' (' + j.all_matches.length + ' triggers matched)' : '');
      st.style.color = 'var(--green,#5c8)';
    }
  } catch (err) { st.textContent = 'Test failed.'; st.style.color = 'var(--red,#e05)'; }
}

// ── QUIPS ─────────────────────────────────────────────────────────────────────
function _sbBuildQuips() {
  const wrap = _el('div');
  const q = _sbData.quips || {};

  wrap.appendChild(_el('p', { class: 'hint', style: 'margin-bottom:10px',
    text: 'Quips are pre-synthesized to a voice cache. Editing a line re-synthesizes it live on save (no restart). One line per row; blank rows are dropped.' }));

  // Wake quips (time-banded)
  wrap.appendChild(_el('h3', { style: 'margin:8px 0 4px;font-size:14px', text: 'Wakeword responses (by hour)' }));
  (q.wake || []).forEach((band, i) => wrap.appendChild(_sbBandCard(band, i)));

  // Simple categories
  wrap.appendChild(_sbSimpleCat('Double-tap retorts', q.double_tap, true));
  wrap.appendChild(_sbSimpleCat('Post-speech retorts', q.post_speech, true));
  wrap.appendChild(_sbSimpleCat('Kids "thinking" fillers', q.kids_fillers, false));

  // Top-of-hour quips
  wrap.appendChild(_sbTopOfHourCard(q.top_of_hour));

  // First-of-day greeting
  wrap.appendChild(_sbFirstOfDayCard(q.first_of_day));

  // Gesture cues
  wrap.appendChild(_sbGestureCard(q.gesture_cues));

  // RPQR timing (advanced)
  wrap.appendChild(_sbTimingCard(q.rpqr_timing));
  return wrap;
}

function _sbCatHeader(title, obj) {
  const h = _el('div', { style: 'display:flex;align-items:center;gap:8px;margin-bottom:4px' });
  const tog = _el('input', { type: 'checkbox', checked: obj.enabled !== false,
    onchange: (ev) => { obj.enabled = ev.target.checked; _sbMarkDirty(); } });
  h.append(tog, _el('span', { style: 'font-weight:600;font-size:13px', text: title }));
  return h;
}

function _sbCard() {
  return _el('div', { style: 'border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:8px' });
}

function _sbLinesArea(obj) {
  const ta = _el('textarea', {
    value: (obj.lines || []).join('\n'), rows: Math.max(3, (obj.lines || []).length + 1),
    style: 'width:100%;box-sizing:border-box;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:4px 6px;font-size:12px;font-family:inherit',
    onchange: (ev) => { obj.lines = ev.target.value.split('\n').map(s => s.trim()).filter(Boolean); _sbMarkDirty(); },
  });
  return ta;
}

function _sbEmotionSelect(obj) {
  const sel = _el('select', {
    style: 'background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 4px;font-size:12px',
    onchange: (ev) => { obj.emotion = ev.target.value; _sbMarkDirty(); },
  });
  _sbMeta.emotions.forEach(e => sel.appendChild(_el('option', { value: e, text: e, selected: obj.emotion === e })));
  return sel;
}

function _sbBandCard(band, i) {
  const card = _sbCard();
  const head = _el('div', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px' });
  head.append(
    _el('input', { type: 'checkbox', checked: band.enabled !== false, title: 'enable band',
      onchange: (ev) => { band.enabled = ev.target.checked; _sbMarkDirty(); } }),
    _el('span', { style: 'font-weight:600;font-size:13px', text: _sbPad(band.hour_start) + ':00–' + _sbPad(band.hour_end) + ':00' }),
    _el('span', { style: 'font-size:11px;color:var(--muted)', text: 'emotion' }),
    _sbEmotionSelect(band),
  );
  card.append(head, _sbLinesArea(band));
  return card;
}

function _sbPad(n) { return (n < 10 ? '0' : '') + n; }

function _sbSimpleCat(title, obj, hasEmotion) {
  const card = _sbCard();
  if (!obj) { card.appendChild(_el('span', { text: title + ' (missing)' })); return card; }
  const head = _sbCatHeader(title, obj);
  if (hasEmotion) {
    head.append(_el('span', { style: 'font-size:11px;color:var(--muted)', text: 'emotion' }), _sbEmotionSelect(obj));
  }
  card.append(head, _sbLinesArea(obj));
  return card;
}

function _sbGestureCard(obj) {
  const card = _sbCard();
  if (!obj) { card.appendChild(_el('span', { text: 'Gesture cues (missing)' })); return card; }
  card.appendChild(_sbCatHeader('Gesture audible cues', obj));
  card.appendChild(_el('p', { class: 'hint', style: 'margin:2px 0 6px', text: 'Spoken when a base-mount gesture fires. Keys are fixed; edit the text.' }));
  const grid = _el('div', { style: 'display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:center' });
  (_sbMeta.gestureKeys.length ? _sbMeta.gestureKeys : Object.keys(obj.cues || {})).forEach(k => {
    grid.appendChild(_el('label', { style: 'font-size:12px;color:var(--muted);font-weight:600', text: k }));
    grid.appendChild(_el('input', { type: 'text', value: (obj.cues || {})[k] || '',
      style: 'width:100%;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:3px 6px;font-size:12px',
      onchange: (ev) => { obj.cues = obj.cues || {}; obj.cues[k] = ev.target.value; _sbMarkDirty(); } }));
  });
  card.appendChild(grid);
  return card;
}

function _sbTopOfHourCard(obj) {
  const card = _sbCard();
  if (!obj) { card.appendChild(_el('span', { text: 'Top-of-hour quips (missing)' })); return card; }
  if (!obj.overrides) obj.overrides = {};
  const head = _sbCatHeader('Top-of-hour quips', obj);
  head.append(_el('span', { style: 'font-size:11px;color:var(--muted)', text: 'emotion' }), _sbEmotionSelect(obj));
  card.appendChild(head);
  card.appendChild(_el('p', { class: 'hint', style: 'margin:2px 0 6px',
    text: "Spoken near the top of each hour. {hour} is replaced by the hour name. A per-hour override replaces the template for that hour; blank = use the template." }));

  const ovInputs = [];
  const _defTmpl = "{hour} o'clock. That's the whole thought.";

  // Template row
  const trow = _el('div', { style: 'display:flex;gap:8px;align-items:center;margin-bottom:6px' });
  trow.append(
    _el('label', { style: 'font-size:11px;color:var(--muted);font-weight:600', text: 'template' }),
    _el('input', { type: 'text', value: obj.template || '', placeholder: _defTmpl,
      style: 'flex:1;min-width:160px;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:3px 6px;font-size:12px',
      onchange: (ev) => {
        obj.template = ev.target.value; _sbMarkDirty();
        const t = obj.template || _defTmpl;
        ovInputs.forEach(({ h, input }) => { input.placeholder = t.replace(/\{hour\}/g, _SB_HOUR_NAMES[h]); });
      } }));
  card.appendChild(trow);

  // Per-hour override grid
  card.appendChild(_el('div', { style: 'font-size:11px;color:var(--muted);margin:4px 0 2px', text: 'Per-hour overrides (blank = template)' }));
  const grid = _el('div', { style: 'display:grid;grid-template-columns:auto 1fr;gap:3px 8px;align-items:center' });
  for (let h = 0; h < 24; h++) {
    grid.appendChild(_el('label', { style: 'font-size:11px;color:var(--muted)', text: _sbPad(h) + ':00 ' + _SB_HOUR_NAMES[h] }));
    const inp = _el('input', { type: 'text', value: (obj.overrides || {})[String(h)] || '',
      placeholder: (obj.template || _defTmpl).replace(/\{hour\}/g, _SB_HOUR_NAMES[h]),
      style: 'width:100%;box-sizing:border-box;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px;font-size:12px',
      onchange: (ev) => {
        const v = ev.target.value.trim();
        if (v) obj.overrides[String(h)] = v; else delete obj.overrides[String(h)];
        _sbMarkDirty();
      } });
    ovInputs.push({ h, input: inp });
    grid.appendChild(inp);
  }
  card.appendChild(grid);
  return card;
}

function _sbFirstOfDayCard(obj) {
  const card = _sbCard();
  if (!obj) { card.appendChild(_el('span', { text: 'First-of-day greeting (missing)' })); return card; }
  const head = _sbCatHeader('First interaction of the day', obj);
  head.append(_el('span', { style: 'font-size:11px;color:var(--muted)', text: 'emotion' }), _sbEmotionSelect(obj));
  card.appendChild(head);
  card.appendChild(_el('p', { class: 'hint', style: 'margin:2px 0 6px',
    text: "Played once on the first wake of a new day. Before the cutoff hour the morning line is used; at or after it, the evening line." }));
  const grid = _el('div', { style: 'display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:center' });
  grid.appendChild(_el('label', { style: 'font-size:11px;color:var(--muted)', text: 'cutoff hour (0–23)' }));
  grid.appendChild(_el('input', { type: 'number', min: '0', max: '23',
    value: (obj.cutoff_hour != null ? obj.cutoff_hour : 9),
    style: 'width:70px;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px;font-size:12px',
    onchange: (ev) => { const v = parseInt(ev.target.value, 10); if (!isNaN(v)) { obj.cutoff_hour = Math.max(0, Math.min(23, v)); _sbMarkDirty(); } } }));
  grid.appendChild(_el('label', { style: 'font-size:11px;color:var(--muted)', text: 'morning line' }));
  grid.appendChild(_el('input', { type: 'text', value: obj.morning || '', placeholder: 'Morning.',
    style: 'width:100%;box-sizing:border-box;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px;font-size:12px',
    onchange: (ev) => { obj.morning = ev.target.value; _sbMarkDirty(); } }));
  grid.appendChild(_el('label', { style: 'font-size:11px;color:var(--muted)', text: 'evening line' }));
  grid.appendChild(_el('input', { type: 'text', value: obj.evening || '', placeholder: 'Finally.',
    style: 'width:100%;box-sizing:border-box;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px;font-size:12px',
    onchange: (ev) => { obj.evening = ev.target.value; _sbMarkDirty(); } }));
  card.appendChild(grid);
  return card;
}

function _sbTimingCard(obj) {
  const card = _sbCard();
  if (!obj) { card.appendChild(_el('span', { text: 'Quip timing (missing)' })); return card; }
  card.appendChild(_el('div', { style: 'font-weight:600;font-size:13px;margin-bottom:4px', text: 'Quip timing windows (advanced)' }));
  card.appendChild(_el('p', { class: 'hint', style: 'margin:2px 0 6px',
    text: 'Controls which retort fires when you wake IRIS. Seconds, except the top-of-hour minute window.' }));
  const grid = _el('div', { style: 'display:grid;grid-template-columns:1fr auto;gap:4px 8px;align-items:center;max-width:440px' });
  const row = (label, key, min, max) => {
    grid.appendChild(_el('label', { style: 'font-size:12px;color:var(--muted)', text: label }));
    grid.appendChild(_el('input', { type: 'number', min: String(min), max: String(max),
      value: (obj[key] != null ? obj[key] : ''),
      style: 'width:100px;background:var(--input-bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 6px;font-size:12px',
      onchange: (ev) => { const v = parseInt(ev.target.value, 10); if (!isNaN(v)) { obj[key] = Math.max(min, Math.min(max, v)); _sbMarkDirty(); } } }));
  };
  row('Double-tap window (s)', 'double_tap_window_s', 1, 3600);
  row('Post-speech window (s)', 'post_speech_window_s', 1, 3600);
  row('Top-of-hour cooldown (s)', 'top_of_hour_cooldown_s', 0, 86400);
  row('Top-of-hour minute window', 'top_of_hour_minute_window', 0, 59);
  card.appendChild(grid);
  return card;
}

// ── SAVE + RESET ──────────────────────────────────────────────────────────────
async function saveSoundboard() {
  const st = document.getElementById('sb-save-status');
  st.textContent = 'Saving...';
  st.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/soundboard/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clips: _sbData.clips, quips: _sbData.quips }),
    });
    const j = await r.json();
    if (!j.ok) { st.textContent = 'Error: ' + (j.error || 'save failed'); st.style.color = 'var(--red,#e05)'; return; }
    _sbDirty = false;
    const b = document.getElementById('sb-save-btn');
    if (b) { b.classList.remove('btn-primary'); b.textContent = 'Save All Changes'; }
    let msg = 'Saved.';
    if (j.sd === false) { msg += ' ⚠ SD persist failed — lost on reboot.'; st.style.color = 'var(--red,#e05)'; }
    else { msg += ' Persisted to SD.'; st.style.color = 'var(--green,#5c8)'; }
    if (j.reloaded === false) msg += ' (assistant reload not confirmed)';
    st.textContent = msg;
    if (j.version) _sbData.version = j.version;
  } catch (e) { st.textContent = 'Save failed: ' + e; st.style.color = 'var(--red,#e05)'; }
}

async function restoreSoundboard() {
  if (!confirm('Undo the last save?\n\nRestores the soundboard to the state from just before your most recent save (from .goldbak).')) return;
  const st = document.getElementById('sb-save-status');
  st.textContent = 'Restoring...';
  st.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/soundboard/restore', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) { st.textContent = 'Restore failed: ' + (j.error || '?'); st.style.color = 'var(--red,#e05)'; return; }
    _sbDirty = false;
    const b = document.getElementById('sb-save-btn');
    if (b) { b.classList.remove('btn-primary'); b.textContent = 'Save All Changes'; }
    let msg = 'Restored last saved state.';
    if (j.sd === false) { msg += ' ⚠ SD persist failed.'; st.style.color = 'var(--red,#e05)'; }
    else { msg += ' Persisted to SD.'; st.style.color = 'var(--green,#5c8)'; }
    if (j.reloaded === false) msg += ' (assistant reload not confirmed)';
    st.textContent = msg;
    fetchSoundboard();
  } catch (e) { st.textContent = 'Restore failed.'; st.style.color = 'var(--red,#e05)'; }
}

async function resetSoundboard() {
  if (!confirm('Reset ALL quips and clips to seed defaults?\n\nAll current edits will be lost. Clips will all be disabled again.')) return;
  const st = document.getElementById('sb-save-status');
  st.textContent = 'Resetting...';
  st.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/soundboard/reset', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) { st.textContent = 'Reset failed: ' + (j.error || '?'); st.style.color = 'var(--red,#e05)'; return; }
    _sbDirty = false;
    const b = document.getElementById('sb-save-btn');
    if (b) { b.classList.remove('btn-primary'); b.textContent = 'Save All Changes'; }
    let msg = 'Reset to defaults.';
    if (j.sd === false) { msg += ' ⚠ SD persist failed.'; st.style.color = 'var(--red,#e05)'; }
    else { msg += ' Persisted to SD.'; st.style.color = 'var(--green,#5c8)'; }
    if (j.reloaded === false) msg += ' (assistant reload not confirmed)';
    st.textContent = msg;
    fetchSoundboard();
  } catch (e) { st.textContent = 'Reset failed.'; st.style.color = 'var(--red,#e05)'; }
}
