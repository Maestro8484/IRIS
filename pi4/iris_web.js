// ── Sleep Animation slider builder ────────────────────────────────────────────
const _SA_SLIDERS = [
  // [group-id, key, label, min, max, step, defaultVal]
  ["sa-stars-warps","speed","Speed",0.2,2.0,0.1,0.85],
  ["sa-stars-warps","starBrightMin","Star brightness min",20,200,5,115],
  ["sa-stars-warps","starBrightMax","Star brightness max",100,255,5,205],
  ["sa-stars-warps","starTwinkleAmp","Star twinkle amp",20,255,5,140],
  ["sa-stars-warps","warpCount","Warp particle count",0,60,2,32],
  ["sa-stars-warps","warpSpeed","Warp speed",5,100,5,28],
  ["sa-stars-warps","warpBright","Warp brightness",40,255,5,175],
  ["sa-shoots","shootCount","Shoot count",0,10,1,4],
  ["sa-shoots","shootSpeed","Shoot speed",5,120,5,38],
  ["sa-shoots","shootLen","Trail length (px)",10,120,5,55],
  ["sa-shoots","shootBright","Shoot brightness",50,255,5,210],
  ["sa-objects","moonR","Moon radius (px)",10,50,1,28],
  ["sa-objects","moonDrift","Moon drift amp (px)",0,15,1,3],
  ["sa-objects","saturnR","Saturn radius (px)",8,35,1,18],
  ["sa-objects","saturnDrift","Saturn drift amp (px)",0,15,1,4],
  ["sa-objects","nebulaAlpha","Nebula alpha",0,120,4,44],
  ["sa-mouth","waveAmp0","Wave amp primary (px)",5,60,1,28],
  ["sa-mouth","waveAmp1","Wave amp secondary (px)",3,40,1,18],
  ["sa-mouth","waveAmp2","Wave amp tertiary (px)",2,25,1,10],
  ["sa-mouth","waveOscAmp","Wave vertical osc (px)",0,60,2,34],
  ["sa-mouth","mouthPulseAlpha","Mouth pulse alpha",20,255,5,140],
  ["sa-mouth","zzzAlpha0","ZZZ alpha (large)",30,255,5,191],
  ["sa-mouth","zzzAlpha1","ZZZ alpha (medium)",30,255,5,158],
  ["sa-mouth","zzzAlpha2","ZZZ alpha (small)",30,255,5,128],
];

function _buildSaSliders(data) {
  _SA_SLIDERS.forEach(([grp, key, lbl, mn, mx, step, def]) => {
    const container = document.getElementById(grp);
    if (!container) return;
    const val = (data && data[key] != null) ? data[key] : def;
    const row = document.createElement('div');
    row.className = 'field-row';
    row.innerHTML =
      `<label style="width:220px">${lbl}</label>` +
      `<input type="range" id="sa-${key}" min="${mn}" max="${mx}" step="${step}" value="${val}"` +
      ` style="width:160px;accent-color:var(--indigo);height:6px;cursor:pointer"` +
      ` oninput="document.getElementById('sa-v-${key}').textContent=this.value;_saCfgSend('${key}',this.value)">` +
      `<span id="sa-v-${key}" style="width:34px;color:var(--text);font-size:13px;flex-shrink:0">${val}</span>`;
    container.appendChild(row);
  });
}

let _saDebounce = {};
function _saCfgSend(key, val) {
  clearTimeout(_saDebounce[key]);
  _saDebounce[key] = setTimeout(() => {
    const numVal = parseFloat(val);
    fetch('/api/sleep_cfg', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({[key]: numVal})
    }).then(r=>r.json()).catch(()=>{});
  }, 180);
}

function _loadSaSliders() {
  fetch('/api/sleep_cfg').then(r=>r.json()).then(d=>{
    _buildSaSliders(d);
  }).catch(()=>{
    _buildSaSliders(null);
  });
}

// Load sliders when Sleep tab is first shown
var _saLoaded = false;
function _saTabHook() { if (!_saLoaded) { _saLoaded = true; _loadSaSliders(); } }

// ── Tab switching ──────────────────────────────────────────────────────────────
function tab(name, btn) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('sec-' + name).classList.add('active');
  btn.classList.add('active');
  attachDefBadges();   // tabs that build controls dynamically get badges too
  if (name === 'logs') fetchLogs();
  if (name === 'audio') refreshVolume();
  if (name === 'system') { pollStatus(); checkSDStatus(); }
  if (name === 'soundboard') fetchSoundboard();
  if (name === 'voice') { loadKokoroVoices(); }
  if (name === 'gandalf') loadVram();
  if (name === 'bench') { fetchBench(); fetchBenchRecent(); }
  if (name === 'gestures') { loadGestureConfig(); loadBargeinConfig(); fetchGestureLog(); loadGestureStats(); }
  if (name === 'eyes') { pollSleepState(); loadEmotionMap(); _syncMouthSliders(); }
  if (name === 'ogle_cal') { loadPsStatus(); loadSensorLeds(); }
  if (name === 'sleep') {
    pollSleepState();
    const ma = document.getElementById('MOUTH_INTENSITY_AWAKE');
    if (ma) document.getElementById('mouth-awake-display').textContent = ma.value;
    const mi = document.getElementById('MOUTH_INTENSITY_IDLE');
    if (mi) document.getElementById('mouth-idle-display').textContent = mi.value;
    const ms = document.getElementById('MOUTH_INTENSITY_SLEEP');
    if (ms) document.getElementById('mouth-sleep-display').textContent = ms.value;
  }
}

// ── Toast ──────────────────────────────────────────────────────────────────────
let _toastTimer = null;
function toast(msg, ok=true, duration=2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = ok ? '#1d4ed8' : '#b91c1c';
  t.classList.add('show');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), duration);
}

// ── SD status bar ──────────────────────────────────────────────────────────────
async function checkSDStatus() {
  try {
    const r = await fetch('/api/sd_status');
    const j = await r.json();
    _updateSDBar(j.synced ? 'synced' : 'dirty',
      j.synced ? 'SD: synced' : 'Unsaved changes — not persisted to SD (will be lost on reboot)');
  } catch(e) {
    _updateSDBar('checking', 'SD status unknown');
  }
}

function _updateSDBar(state, text) {
  const bar = document.getElementById('sd-bar');
  const txt = document.getElementById('sd-status-text');
  const sys = document.getElementById('sys-sd-status');
  bar.className = 'sd-bar ' + state;
  txt.textContent = text;
  if (sys) {
    sys.textContent = state === 'synced' ? 'synced' : state === 'dirty' ? 'not persisted' : '--';
    sys.style.color = state === 'synced' ? 'var(--green)' : state === 'dirty' ? 'var(--amber)' : 'var(--muted)';
  }
}

async function persistToSD() {
  _updateSDBar('checking', 'Persisting to SD…');
  try {
    const r = await fetch('/api/persist_config', {method: 'POST'});
    const j = await r.json();
    if (j.ok) {
      _updateSDBar('synced', 'SD: synced — persisted ' + new Date().toLocaleTimeString());
      toast('Config persisted to SD card', true, 4000);
    } else {
      _updateSDBar('error', 'Persist FAILED: ' + (j.error || 'unknown error'));
      toast('Persist failed: ' + (j.error || 'error'), false, 5000);
    }
  } catch(e) {
    _updateSDBar('error', 'Persist error: ' + e);
    toast('Persist error', false);
  }
}

// ── Config load/save ──────────────────────────────────────────────────────────
let _cfg = {};
// ── S201b: display-unit helpers (backend keys stay ms / Celsius; UI shows s / F) ──
const _MS_FIELDS = ['ENDPOINT_BASELINE_MS','ENDPOINT_ONSET_MIN_MS','KIDS_THINK_FILLER_MS','KIDS_THINK_FILLER2_MS'];
const _PS_SCALE = { LOST_MS: 1000 };   // display seconds -> wire ms for these PS_CFG keys
const _psToDisp = (key, wire) => wire / (_PS_SCALE[key] || 1);
const _psToWire = (key, disp) => _PS_SCALE[key] ? Math.round(disp * _PS_SCALE[key]) : disp;
const _tempF = c => { const n = parseFloat(c); return isNaN(n) ? '--' : Math.round(n * 9 / 5 + 32) + 'F'; };

async function loadConfig() {
  const r = await fetch('/api/config');
  _cfg = await r.json();
  for (const [k, v] of Object.entries(_cfg)) {
    const el = document.getElementById(k);
    if (!el) continue;
    // Real JSON booleans (true/false) have to be mapped onto the 0/1 option
    // values these selects use, or the browser finds no matching <option> and
    // silently falls back to the FIRST one. Live proof at S224d:
    // CONVO_SESSION_ENABLED is true and the WebUI was showing it as "Off".
    // Older keys stored 1/0 as ints and displayed fine, which is why this hid.
    if (el.tagName === 'SELECT') el.value = (v === true) ? '1' : (v === false) ? '0' : String(v);
    else if (_MS_FIELDS.includes(k)) el.value = v / 1000;   // S201b: ms key shown in seconds
    else el.value = v;
  }
  // Sync mouth-intensity sliders + value labels on both the Sleep and Face tabs
  _syncMouthSliders();
  // Show active wakeword model name
  const wakeLabel = document.getElementById('wakeword-model-label');
  if (wakeLabel && _cfg.WAKE_WORD) wakeLabel.textContent = _cfg.WAKE_WORD;
  // Pre-select current default eye
  const defEyeSel = document.getElementById('default-eye-sel');
  if (defEyeSel && _cfg.DEFAULT_EYE_IDX !== undefined) defEyeSel.value = String(_cfg.DEFAULT_EYE_IDX);
  // S201: bool selects the generic loop mis-maps (config returns a JS bool; options are '0'/'1')
  _kidsBoolSel('ENDPOINT_DEBUG', _cfg.ENDPOINT_DEBUG);
  // S214: AEC bool selects (same 0/1-vs-JS-bool mismatch as ENDPOINT_DEBUG)
  _kidsBoolSel('BARGEIN_AEC_ENABLED',      _cfg.BARGEIN_AEC_ENABLED);
  _kidsBoolSel('BARGEIN_PRESENCE_ENABLED', _cfg.BARGEIN_PRESENCE_ENABLED);
  _kidsBoolSel('BARGEIN_PRESENCE_KIDS',    _cfg.BARGEIN_PRESENCE_KIDS);
  _kidsBoolSel('AEC_DEBUG',                _cfg.AEC_DEBUG);
  // S220b: conversation-session/trajectory bool selects (same mismatch)
  _kidsBoolSel('CONVO_SESSION_ENABLED', _cfg.CONVO_SESSION_ENABLED);
  _kidsBoolSel('TRAJECTORY_ENABLED',    _cfg.TRAJECTORY_ENABLED);
  _kidsBoolSel('TRAJECTORY_THREADS_ENABLED', _cfg.TRAJECTORY_THREADS_ENABLED);
  _kidsBoolSel('TRAJECTORY_DEBUG',      _cfg.TRAJECTORY_DEBUG);
  // RD-068: weather master flag (same JS-bool-vs-'0'/'1' mismatch)
  _kidsBoolSel('WEATHER_ENABLED',       _cfg.WEATHER_ENABLED);
  _kidsBoolSel('WEATHER_MISS_CLAUSE',   _cfg.WEATHER_MISS_CLAUSE);
}

async function saveFields(keys) {
  const patch = {};
  for (const k of keys) {
    const el = document.getElementById(k);
    if (!el) continue;
    const raw = el.value;
    if (_MS_FIELDS.includes(k)) patch[k] = Math.round(Number(raw) * 1000);   // S201b: seconds -> ms key
    else patch[k] = isNaN(raw) || raw === '' ? raw : Number(raw);
  }
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)});
  const j = await r.json();
  toast(j.ok ? 'Saved to RAM' : 'Error', j.ok);
  if (j.ok) checkSDStatus();
}

// S215 personality continuum: recenters all three sliders (0.5 = detent 0 =
// empty steering clause = exactly standard IRIS) and saves in one call.
async function resetPersonality() {
  for (const k of ['PERSONA_TONE_KIDS', 'PERSONA_TONE_ADULT', 'PERSONA_ENGAGE']) {
    const el = document.getElementById(k);
    if (el) el.value = 0.5;
  }
  await saveFields(['PERSONA_TONE_KIDS', 'PERSONA_TONE_ADULT', 'PERSONA_ENGAGE']);
}

async function saveDefaultEye() {
  const sel = document.getElementById('default-eye-sel');
  if (!sel) return;
  const idx = parseInt(sel.value);
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({DEFAULT_EYE_IDX: idx})});
  const j = await r.json();
  toast(j.ok ? 'Default eye saved — applies on next IRIS restart' : 'Save failed', j.ok);
  if (j.ok) checkSDStatus();
}

// ── Teensy ─────────────────────────────────────────────────────────────────────
async function loadKokoroVoices() {
  const input = document.getElementById('KOKORO_VOICE');
  const pick  = document.getElementById('KOKORO_VOICE_PICK');
  const blendA = document.getElementById('BLEND_A');
  const blendB = document.getElementById('BLEND_B');
  if (!input) return;
  // Always show the exact live value first -- fixes the bug where a blend not
  // present in the single-voice list left the old <select> defaulted to its
  // first option (af_alloy) and a Save silently clobbered the blend. (S175)
  const current = (_cfg && _cfg.KOKORO_VOICE) ? _cfg.KOKORO_VOICE : 'bm_lewis';
  input.value = current;
  if (pick) pick.innerHTML = '<option>Loading...</option>';
  try {
    const r = await fetch('/api/kokoro_voices');
    const j = await r.json();
    const voices = j.voices || [];
    if (!voices.length) {
      if (pick) pick.innerHTML = '<option value="">No voices found</option>';
      return;
    }
    const optsHtml = voices.map(function(name) {
      return '<option value="' + name + '">' + name + '</option>';
    }).join('');
    if (pick) pick.innerHTML = '<option value="">-- pick to overwrite Voice field --</option>' + optsHtml;
    if (blendA) blendA.innerHTML = optsHtml;
    if (blendB) blendB.innerHTML = optsHtml;
    // If the live value is already a 2-voice blend, pre-select the builder to match
    const m = /^([a-z]+_[a-z]+)\(([\d.]+)\)\+([a-z]+_[a-z]+)\(([\d.]+)\)$/i.exec(current);
    if (m && blendA && blendB) {
      blendA.value = m[1]; document.getElementById('BLEND_A_W').value = m[2];
      blendB.value = m[3]; document.getElementById('BLEND_B_W').value = m[4];
    }
  } catch(e) { if (pick) pick.innerHTML = '<option>Kokoro offline</option>'; }
}

// Persistent IRIS voice presets: value = "VOICE|SPEED". Sets the Voice + speed
// fields (does not save/persist -- user still clicks Save Kokoro Settings).
function applyVoicePreset(val) {
  if (!val) return;
  const parts = val.split('|');
  const v = document.getElementById('KOKORO_VOICE');
  const s = document.getElementById('KOKORO_SPEED');
  if (v && parts[0]) v.value = parts[0];
  if (s && parts[1]) s.value = parts[1];
  toast('Preset loaded — click Save Kokoro Settings to apply', true);
}

// Audition the CURRENT (possibly unsaved) Voice + speed field values by speaking
// a sample line on IRIS. Uses the /api/speak voice/speed override (live config
// untouched). Lets you compare a blend before committing it.
async function previewVoice() {
  const voice = document.getElementById('KOKORO_VOICE').value.trim();
  const speedEl = document.getElementById('KOKORO_SPEED');
  const speed = speedEl ? parseFloat(speedEl.value) || 0.95 : 0.95;
  if (!voice) { toast('Voice field is empty', false); return; }
  const text = "Well... look who finally decided to show up. But no matter -- I'm listening now.";
  try {
    const r = await fetch('/api/speak', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text, voice, speed})});
    const j = await r.json();
    toast(j.ok ? 'Previewing on IRIS speaker...' : ('Preview error: ' + (j.error||'?')), j.ok);
  } catch(e) { toast('Preview error: ' + e, false); }
}

function applyVoiceBlend() {
  const a  = document.getElementById('BLEND_A').value;
  const b  = document.getElementById('BLEND_B').value;
  const wA = parseFloat(document.getElementById('BLEND_A_W').value) || 0;
  const wB = parseFloat(document.getElementById('BLEND_B_W').value) || 0;
  if (!a || !b) { toast('Pick both blend voices first', false); return; }
  document.getElementById('KOKORO_VOICE').value = a + '(' + wA + ')+' + b + '(' + wB + ')';
}

async function saveKokoroSettings() {
  const enabled = document.getElementById('KOKORO_ENABLED').value === 'true';
  const voice   = document.getElementById('KOKORO_VOICE').value.trim();
  const speedEl = document.getElementById('KOKORO_SPEED');
  const speed   = speedEl ? Math.max(0.5, Math.min(2.0, parseFloat(speedEl.value) || 1.0)) : 1.0;
  if (!voice) { toast('Voice field is empty -- refusing to save', false); return; }
  const r = await fetch('/api/config', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({KOKORO_ENABLED: enabled, KOKORO_VOICE: voice, KOKORO_SPEED: speed})});
  const j = await r.json();
  toast(j.ok ? 'Kokoro settings saved' : 'Error', j.ok);
  if (j.ok) persistToSD();
}

async function sendTeensy(cmd) {
  const r = await fetch('/api/teensy', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({cmd})});
  const j = await r.json();
  toast(j.ok ? cmd : 'Teensy error: ' + cmd, j.ok);
}

// ── Sleep control ──────────────────────────────────────────────────────────────
let _isSleeping = false;

function updateSleepUI(sleeping) {
  _isSleeping = sleeping;
  const dot    = document.getElementById('sleep-dot');
  const lbl    = document.getElementById('sleep-label');
  const btnS   = document.getElementById('btn-sleep');
  const btnW   = document.getElementById('btn-wake');
  const hdrLbl = document.getElementById('lbl-sleep-hdr');
  const sysSleep = document.getElementById('sys-sleep');

  if (sleeping) {
    dot.classList.add('sleeping');
    lbl.textContent = 'IRIS is sleeping — starfield active, mouth snoring';
    lbl.style.color = 'var(--indigo)';
    btnS.classList.add('active-state');
    btnW.classList.remove('active-state');
    btnW.style.background = '#1d4ed8';
    btnW.style.color = '#fff';
    if (hdrLbl) hdrLbl.style.display = 'inline';
    if (sysSleep) { sysSleep.textContent = 'sleeping'; sysSleep.style.color = 'var(--indigo)'; }
  } else {
    dot.classList.remove('sleeping');
    lbl.textContent = 'IRIS is awake';
    lbl.style.color = 'var(--text)';
    btnS.classList.remove('active-state');
    btnW.classList.add('active-state');
    btnW.style.background = '#14532d';
    btnW.style.color = 'var(--green)';
    if (hdrLbl) hdrLbl.style.display = 'none';
    if (sysSleep) { sysSleep.textContent = 'awake'; sysSleep.style.color = 'var(--green)'; }
  }
}

async function pollSleepState() {
  try {
    const r = await fetch('/api/sleep_state');
    const j = await r.json();
    updateSleepUI(j.sleeping);
  } catch(e) {}
}

async function triggerSleep() {
  const r = await fetch('/api/sleep', {method:'POST'});
  const j = await r.json();
  if (j.ok) { await pollSleepState(); toast('IRIS sleeping'); }
  else toast('Sleep command failed', false);
}

async function triggerWake() {
  const r = await fetch('/api/wake', {method:'POST'});
  const j = await r.json();
  if (j.ok) { await pollSleepState(); toast('IRIS awake'); }
  else toast('Wake command failed', false);
}

// ── Mouth intensity ────────────────────────────────────────────────────────────
// The Sleep tab and the Face tab each carry an identical "TFT Mouth Intensity" card
// (Face-tab ids are suffixed "_F"). Both edit the same MOUTH_INTENSITY_* config keys;
// _syncMouthSliders keeps the two sets and their value labels in step. Call with no
// args to read the current base-slider values (e.g. after loadConfig / on tab show),
// or pass {AWAKE,IDLE,SLEEP} to force a set (e.g. right after a save).
function _syncMouthSliders(vals) {
  ['AWAKE','IDLE','SLEEP'].forEach(function(kind) {
    const base = document.getElementById('MOUTH_INTENSITY_' + kind);
    const face = document.getElementById('MOUTH_INTENSITY_' + kind + '_F');
    let v = (vals && vals[kind] !== undefined) ? String(vals[kind])
            : (base ? base.value : (face ? face.value : null));
    if (v === null) return;
    if (base) base.value = v;
    if (face) face.value = v;
    const lbl = 'mouth-' + kind.toLowerCase() + '-display';
    const bd = document.getElementById(lbl);
    const fd = document.getElementById(lbl + '-f');
    if (bd) bd.textContent = v;
    if (fd) fd.textContent = v;
  });
}

// sfx = '' for the Sleep-tab card, '_F' for the Face-tab card.
async function saveMouthIntensity(sfx) {
  sfx = sfx || '';
  const g = (kind) => Math.max(0, Math.min(15, parseInt(
      document.getElementById('MOUTH_INTENSITY_' + kind + sfx).value)));
  const awake = g('AWAKE'), idle = g('IDLE'), sleep = g('SLEEP');
  await fetch('/api/config', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({MOUTH_INTENSITY_AWAKE: awake, MOUTH_INTENSITY_IDLE: idle, MOUTH_INTENSITY_SLEEP: sleep})});
  // When awake the mouth rests at the idle level between interactions — push that
  // so the slider gives immediate feedback on the resting brightness being tuned.
  const intensity = _isSleeping ? sleep : idle;
  await fetch('/api/teensy', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd: 'MOUTH_INTENSITY:' + intensity})});
  _syncMouthSliders({AWAKE: awake, IDLE: idle, SLEEP: sleep});
  toast('Mouth intensity saved and applied');
  checkSDStatus();
}

// ── Logs ───────────────────────────────────────────────────────────────────────
let _logFilter = 'all';
let _logAutoTimer = null;
let _logEvents = [];

const _CAT_LABELS = {
  wakeword:'WAKE', stt:'HEARD', route:'ROUTE', llm:'LLM',
  tts:'SPOKEN', stop:'STOP', drift:'DRIFT', error:'ERR',
  info:'INFO', cmd:'CMD', warn:'WARN', gesture:'GESTURE'
};

function _esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setLogFilter(cat, btn) {
  _logFilter = cat;
  document.querySelectorAll('.btn-filter').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderLogEvents();
}

function renderLogEvents() {
  const box = document.getElementById('log-events');
  const cnt = document.getElementById('log-count');
  const evs = _logFilter === 'all' ? _logEvents
             : _logEvents.filter(e => e.cat === _logFilter);
  if (cnt) cnt.textContent = evs.length + ' event' + (evs.length !== 1 ? 's' : '');
  if (!evs.length) {
    box.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">No events in this category</div>';
    return;
  }
  // Newest first
  box.innerHTML = evs.slice().reverse().map(e => {
    const label  = _CAT_LABELS[e.cat] || (e.cat||'?').toUpperCase();
    const detail = e.detail ? `<span class="log-detail">${_esc(e.detail)}</span>` : '';
    return `<div class="log-event cat-${_esc(e.cat||'info')}">` +
           `<span class="log-ts">${_esc(e.ts)}</span>` +
           `<span class="log-cat">[${label}]</span>` +
           `<span class="log-msg" title="${_esc(e.msg)}">${_esc(e.msg)}</span>` +
           `${detail}</div>`;
  }).join('');
  window.requestAnimationFrame(function() { box.scrollTop = 0; });
}

async function fetchLogs() {
  const box = document.getElementById('log-events');
  box.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">Loading...</div>';
  try {
    const r = await fetch('/api/logs');
    const j = await r.json();
    _logEvents = j.events || [];
    renderLogEvents();
  } catch(e) {
    box.innerHTML = `<div style="color:var(--red);padding:12px">Error: ${_esc(String(e))}</div>`;
  }
}

function toggleLogsAuto(cb) {
  if (_logAutoTimer) { clearInterval(_logAutoTimer); _logAutoTimer = null; }
  if (cb.checked) _logAutoTimer = setInterval(fetchLogs, 15000);
}

// ── Status ─────────────────────────────────────────────────────────────────────
async function pollStatus() {
  const r = await fetch('/api/status');
  const j = await r.json();
  const dot = document.getElementById('dot-assistant');
  const lbl = document.getElementById('lbl-assistant');
  document.getElementById('lbl-temp').textContent = _tempF(j.cpu_temp);
  document.getElementById('lbl-uptime').textContent = j.uptime;
  dot.className = 'dot' + (j.running ? ' on' : '');
  lbl.textContent = j.running ? 'running' : 'stopped';
  const sr = document.getElementById('sys-running');
  const st = document.getElementById('sys-temp');
  const su = document.getElementById('sys-uptime');
  if(sr) { sr.textContent = j.running ? 'running' : 'stopped'; sr.style.color = j.running ? 'var(--green)' : 'var(--red)'; }
  if(st) st.textContent = _tempF(j.cpu_temp);
  if(su) su.textContent = j.uptime;
  if (typeof j.sleeping === 'boolean') updateSleepUI(j.sleeping);
  pollHealth();
}

// S192m AUD-12/B4: main-loop liveness heartbeat (green <30s, amber <120s, red older/missing)
async function pollHealth() {
  const el = document.getElementById('sys-heartbeat');
  if (!el) return;
  try {
    const j = await (await fetch('/api/health')).json();
    if (!j.heartbeat_found || j.heartbeat_age_s == null) {
      el.textContent = 'no heartbeat found';
      el.style.color = 'var(--red)';
      return;
    }
    const age = j.heartbeat_age_s;
    const st = j.state || '?';
    // State-aware coloring (S193): 'waiting' is the parked-in-wakeword-wait idle state
    // and is healthy no matter how long since the last interaction, so it must NEVER go
    // red (the old age-only rule painted a normal idle robot red). Red is reserved for a
    // 'processing' turn that has stalled — that should complete in seconds.
    let color, hint = '';
    if (st === 'waiting') {
      color = age < 30 ? 'var(--green)' : 'var(--amber, #d8a200)';
      if (age >= 120) hint = ' — idle (normal: waiting for wakeword)';
    } else if (st === 'processing') {
      color = age < 15 ? 'var(--green)' : (age < 60 ? 'var(--amber, #d8a200)' : 'var(--red)');
      if (age >= 60) hint = ' — possible stall';
    } else {
      color = age < 30 ? 'var(--green)' : (age < 120 ? 'var(--amber, #d8a200)' : 'var(--red)');
    }
    el.textContent = `alive ${age}s ago (${st}, loop ${j.loop_count ?? '?'}, oww restarts ${j.oww_restarts ?? '?'})${hint}`;
    el.style.color = color;
  } catch (e) {
    el.textContent = 'unavailable';
    el.style.color = 'var(--red)';
  }
}

// ── Resource Monitor (RD-032) ───────────────────────────────────────────────────
function _pctColor(s, warn, crit) {
  const n = parseInt(s, 10);
  if (isNaN(n)) return 'var(--muted)';
  if (n >= crit) return 'var(--red)';
  if (n >= warn) return 'var(--amber, #d8a200)';
  return 'var(--green)';
}
function _drawSpark(id, vals) {
  const c = document.getElementById(id);
  if (!c || !c.getContext) return;
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 3;
  ctx.clearRect(0, 0, W, H);
  const nums = vals.map(v => parseFloat(v)).filter(v => !isNaN(v));
  if (nums.length < 2) return;
  const min = Math.min(...nums), max = Math.max(...nums), span = (max - min) || 1;
  ctx.beginPath();
  ctx.strokeStyle = 'var(--blue)';
  ctx.lineWidth = 1.5;
  nums.forEach((v, i) => {
    const x = pad + (W - 2 * pad) * (i / (nums.length - 1));
    const y = H - pad - (H - 2 * pad) * ((v - min) / span);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}
// ── Versions / About (RD-048, S213) ────────────────────────────────────────────
// Renders /api/version. Every value is self-reported by the live component;
// staleness colours follow the sys-heartbeat pattern (green fresh / amber
// stale-or-preversioning / red unknown-or-mismatch).
async function pollVersion() {
  let j;
  try { j = await (await fetch('/api/version')).json(); }
  catch (e) { return; }
  const set = (id, txt, color) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    if (color) el.style.color = color;
  };
  const AMBER = 'var(--amber, #d8a200)';
  const _ageStr = s => s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m`
                     : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
  const _fwRow = (c, age, expected) => {
    if (!c || !c.firmware) return ['not reported yet (arrives with the Teensy boot banner)', 'var(--red)'];
    let txt = c.firmware + (c.built ? ` (built ${c.built})` : '');
    if (age != null) txt += ` — reported ${_ageStr(age)} ago`;
    if (c.proto == null) return [txt + ' — no proto (pre-S213 firmware)', AMBER];
    if (c.proto !== expected) return [txt + ` — PROTO MISMATCH fw=${c.proto} pi=${expected}`, 'var(--red)'];
    return [txt, 'var(--green)'];
  };
  const [et, ec] = _fwRow(j.eyes, j.eyes_age_s, j.eyes_expected_proto);
  set('ver-eyes', et, ec);
  const [st2, sc2] = _fwRow(j.servo, j.servo_age_s, j.servo_expected_proto);
  set('ver-servo', st2, sc2);
  const pe = (j.eyes && j.eyes.proto != null) ? j.eyes.proto : '?';
  const ps = (j.servo && j.servo.proto != null) ? j.servo.proto : '?';
  const protoOk = pe === j.eyes_expected_proto && ps === j.servo_expected_proto;
  set('ver-proto', `eyes ${pe}/${j.eyes_expected_proto} · servo ${ps}/${j.servo_expected_proto}`,
      protoOk ? 'var(--green)' : AMBER);
  const p4 = j.pi4 || {};
  set('ver-pi4', `assistant.py ${p4.assistant_py || '?'} · iris_web.py ${p4.iris_web_py || '?'}`,
      (p4.assistant_py && p4.iris_web_py) ? 'var(--text)' : AMBER);
  // A tag is not a model name (WEBUI_TODEPLOY item 1, S244). Both LLM rows say
  // what each tag is BUILT FROM; the base comes from ollama's own
  // details.parent_model, so it cannot drift the way a typed string would.
  const _llmRow = (m) => {
    if (!m) return ['unreachable (GandalfAI asleep — normal; wakes on wakeword)', AMBER];
    if (m.missing) return [`${m.tag} — NOT on GandalfAI (config names a model that does not exist there)`, 'var(--red)'];
    const spec = [m.params, m.quant].filter(Boolean).join(' ');
    const what = m.derived ? `→ ${m.base}` : `→ ${m.base} (a base model, no IRIS layer)`;
    return [`${m.tag} ${what}${spec ? ' · ' + spec : ''} · build ${m.digest}`, 'var(--green)'];
  };
  const ol = j.ollama;
  const [lat, lac] = _llmRow(ol && ol.adult);
  set('ver-llm-adult', lat, lac);
  const [lkt, lkc] = _llmRow(ol && ol.kids);
  set('ver-llm-kids', lkt, lkc);
  if (!ol) {
    set('ver-llm-other', 'unreachable (GandalfAI asleep)', AMBER);
  } else {
    // Names only, never sizes: these all share the base weights file, so listing
    // a size per tag would imply disk use that is not there.
    const ot = ol.other_iris_tags || [];
    set('ver-llm-other', ot.length
        ? `${ot.length} staging tag${ot.length === 1 ? '' : 's'}, all sharing the same weights: ${ot.join(', ')}`
        : 'none', 'var(--muted)');
  }
  const kk = j.kokoro || {};
  set('ver-kokoro', kk.voice ? `${kk.voice} @ ${kk.speed}` : '?', 'var(--text)');
}

async function pollSysstat() {
  let j;
  try { j = await (await fetch('/api/sysstat')).json(); }
  catch (e) { return; }
  const set = (id, txt, color) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    if (color) el.style.color = color;
  };
  set('ss-overlay', j.overlay_pct || '?', _pctColor(j.overlay_pct, 75, 90));
  set('ss-sd',      j.sd_pct || '?',      _pctColor(j.sd_pct, 75, 90));
  const jn = parseFloat(j.journal) || 0;
  set('ss-journal', (j.journal || '?') + ' / 50M cap', jn >= 45 ? 'var(--amber,#d8a200)' : 'var(--text)');
  const lm = parseInt(j.logs_mb, 10) || 0;
  set('ss-logs',    (j.logs_mb || '?') + 'M / 100M cap', lm >= 90 ? 'var(--amber,#d8a200)' : 'var(--text)');
  set('ss-load',    (j.load || []).join(' / '), 'var(--text)');
  set('ss-mem',     `${j.mem_used_mb}M used / ${j.mem_avail_mb}M avail / ${j.mem_total_mb}M`, 'var(--text)');
  set('ss-temp',    (j.temp_c != null ? _tempF(j.temp_c) : '?'),
                    (j.temp_c >= 70 ? 'var(--red)' : 'var(--text)'));
  set('ss-throttle', j.throttled || '?',
                    (j.throttled && j.throttled !== '0x0') ? 'var(--red)' : 'var(--green)');
  set('ss-uptime',  j.uptime || '?', 'var(--text)');
  _drawSpark('ss-spark', (j.trend || []).map(t => t.journalMB));
}

async function restartAssistant() {
  await fetch('/api/restart', {method:'POST'});
  toast('Restarting IRIS...');
  setTimeout(pollStatus, 3000);
}

// ── VRAM ───────────────────────────────────────────────────────────────────────
async function loadVram() {
  const box = document.getElementById('vram-box');
  box.textContent = 'Loading...';
  // Same call fills the Ollama Models card above: a tag is not a model name
  // (WEBUI_TODEPLOY item 1, S244), so both cards say what the tag resolves to.
  const setGm = (id, txt, color) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = txt;
    el.style.color = color;
  };
  const _actRow = (m) => {
    if (!m) return ['GandalfAI asleep, cannot confirm what this tag resolves to', 'var(--amber,#d8a200)'];
    if (m.missing) return [`"${m.tag}" is NOT on GandalfAI — nothing will answer`, 'var(--red)'];
    const spec = [m.params, m.quant].filter(Boolean).join(' ');
    const what = m.derived ? `built on ${m.base}` : `is itself the base model`;
    return [`${m.resolved} → ${what}${spec ? ' · ' + spec : ''}`, 'var(--green)'];
  };
  try {
    const r = await fetch('/api/vram');
    const j = await r.json();
    const act = j.active || {};
    const [ta, ca] = _actRow(j.reachable ? act.adult : null);
    setGm('gm-adult', ta, ca);
    const [tk, ck] = _actRow(j.reachable ? act.kids : null);
    setGm('gm-kids', tk, ck);
    if (j.error) { box.textContent = 'Gandalf offline: ' + j.error; return; }
    const models = j.models || [];
    if (!models.length) { box.textContent = 'No models loaded in VRAM'; return; }
    box.textContent = models.map(m => {
      const spec = [m.params, m.quant].filter(Boolean).join(' ');
      const base = m.derived ? `\n  built on: ${m.base}${spec ? '  (' + spec + ')' : ''}`
                 : (spec ? `\n  base model (no IRIS layer)  (${spec})` : '');
      const ctx  = m.context_length ? `  context: ${m.context_length}` : '';
      return `${m.name}${base}\n  size: ${(m.size/1e9).toFixed(1)} GB  vram: ${(m.size_vram/1e9).toFixed(1)} GB${ctx}`;
    }).join('\n\n');
  } catch(e) { box.textContent = 'Error: ' + e; }
}

// ── Chat ───────────────────────────────────────────────────────────────────────
let _chatMode    = 'silent';   // 'silent' | 'speak' | 'verbatim'
let _chatPersona = 'adult';
// RD-064: prior typed turns, sent to /api/chat as `history` so typed multi-turn
// chat carries context (and episodic recall) exactly like the voice path. The
// server is stateless -- it holds no history of its own. Cleared by clearChat().
let _chatTranscript = [];

const _CHAT_MODE_HINTS = {
  silent:   '',
  speak:    'IRIS will generate a response via LLM and speak it aloud. May conflict with active voice pipeline.',
  verbatim: 'IRIS will speak your exact text through TTS — no LLM. Use when voice pipeline is idle.'
};

function updateChatMode(radio) {
  _chatMode = radio.value;
  const hint = document.getElementById('chat-mode-hint');
  if (hint) hint.textContent = _CHAT_MODE_HINTS[_chatMode] || '';
}

async function sendChat() {
  const inp  = document.getElementById('chat-input');
  const box  = document.getElementById('chat-box');
  const text = inp.value.trim();
  if (!text) return;
  const persona = document.querySelector('input[name="chat-persona"]:checked');
  _chatPersona = persona ? persona.value : 'adult';
  inp.value = '';

  const userMsg = document.createElement('div');
  userMsg.className = 'chat-msg user';
  userMsg.textContent = 'You: ' + text;
  box.appendChild(userMsg);
  box.scrollTop = box.scrollHeight;

  if (_chatMode === 'verbatim') {
    const out = document.createElement('div');
    out.className = 'chat-msg iris';
    out.textContent = 'IRIS [verbatim]: ' + text;
    box.appendChild(out);
    box.scrollTop = box.scrollHeight;
    try {
      const r = await fetch('/api/speak', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text})});
      const j = await r.json();
      if (!j.ok) { out.className = 'chat-msg err'; out.textContent = 'Speak error: ' + (j.error||'unknown'); }
    } catch(e) {
      out.className = 'chat-msg err';
      out.textContent = 'Speak error: ' + e;
    }
    return;
  }

  const thinking = document.createElement('div');
  thinking.className = 'chat-msg iris';
  thinking.textContent = _chatMode === 'speak' ? 'IRIS: thinking (will speak)...' : 'IRIS: thinking...';
  box.appendChild(thinking);
  try {
    const r = await fetch('/api/chat', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text, speak: _chatMode === 'speak', mode: _chatPersona, history: _chatTranscript})});
    const j = await r.json();
    if (j.reply) {
      const spokenTag  = j.spoken  ? ' [spoken]'           : '';
      const emotionTag = j.emotion ? ` {${j.emotion}}`     : '';
      thinking.textContent = 'IRIS' + spokenTag + emotionTag + ': ' + j.reply;
      // RD-064: record the turn so the next typed message carries this context.
      _chatTranscript.push({role:'user', content:text}, {role:'assistant', content:j.reply});
      if (_chatTranscript.length > 40) _chatTranscript = _chatTranscript.slice(-40);
    } else {
      thinking.className = 'chat-msg err';
      thinking.textContent = 'Error: ' + (j.error || 'unknown');
    }
  } catch(e) {
    thinking.className = 'chat-msg err';
    thinking.textContent = 'Error: ' + e;
  }
  box.scrollTop = box.scrollHeight;
}

function clearChat() {
  document.getElementById('chat-box').innerHTML = '';
  _chatTranscript = [];   // RD-064: a cleared box starts a fresh conversation
}

// ── Vision Demo ───────────────────────────────────────────────────────────────
async function sendVision(prompt) {
  prompt = (prompt || '').trim();
  if (!prompt) { toast('Enter a prompt', false); return; }
  const resultBox  = document.getElementById('vision-result');
  const statusEl   = document.getElementById('vision-status');
  const speakCheck = document.getElementById('vision-speak');
  resultBox.style.display = 'none';
  resultBox.textContent   = '';
  statusEl.textContent    = 'Capturing frame and querying vision model...';
  try {
    const r = await fetch('/api/vision', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt, speak: speakCheck && speakCheck.checked})
    });
    const j = await r.json();
    if (j.error) {
      statusEl.textContent = 'Error: ' + j.error;
      statusEl.style.color = 'var(--red)';
    } else {
      resultBox.textContent   = j.reply || '(no reply)';
      resultBox.style.display = 'block';
      const spokenTag = j.spoken ? ' — speaking via Kokoro' : '';
      statusEl.textContent  = 'Done' + spokenTag + (j.emotion ? '  {' + j.emotion + '}' : '');
      statusEl.style.color  = 'var(--muted)';
    }
  } catch(e) {
    statusEl.textContent = 'Request failed: ' + e;
    statusEl.style.color = 'var(--red)';
  }
}

// ── Emotion Display Mapping ────────────────────────────────────────────────────
const _EMOTION_NAMES = ['NEUTRAL','HAPPY','CURIOUS','ANGRY','SLEEPY','SURPRISED','SAD','CONFUSED','AMUSED',
  'ANNOYED','EXASPERATED'];
// S241: ONE eye-name table drives both the Eye Style button grid and the
// Emotion Display eye dropdown. Order and index must match eyeDefinitions in
// src/config.h -- adding a firmware eye is then a one-line edit here.
// A star marks the sets authored as asymmetric left/right pairs.
const _EYE_NAMES = ['Nordic Blue','Flame','Hypno Red','Hazel','Blue Flame 1','Dragon','Striking Blue',
  'Cat','Doom Spiral *','Anime *','Doe *','Demon *','Skull','Leopard *','Toon Stripe','Fizzgig',
  'Newt','Snake','Fish','Brown','Big Blue','Spikes','Firebox','Blue Flame 2','Doom Red'];
const _EYE_OPT = [[-1,'Default (auto)']].concat(_EYE_NAMES.map((n,i)=>[i, i+' - '+n]));

// Fill the Eye Style grid from _EYE_NAMES (the buttons used to be hand-written
// markup capped at index 6, which silently hid every new firmware eye).
function _buildEyeGrid() {
  const grid = document.getElementById('eye-style-grid');
  if (!grid) return;
  grid.innerHTML = _EYE_NAMES.map((n,i)=>
    `<button class="btn-eye" onclick="sendTeensy('EYE:${i}')">${i} - ${n}</button>`).join('');
}
const _MOUTH_OPT = [[0,'0 - Neutral'],[1,'1 - Happy'],[2,'2 - Curious'],[3,'3 - Angry'],
  [4,'4 - Sleepy'],[5,'5 - Surprised'],[6,'6 - Sad'],[7,'7 - Confused'],
  [8,'8 - Sleep'],[9,'9 - Silly (tongue)']];

let _emotionMap = {mouth_map:{}, eye_map:{}};

// S242: what each emotion does to the EYELIDS, transcribed from the S241 lid
// choreographer table (src/main.cpp lidScripts[EMOTION_COUNT]). Shown so the
// operator can see the whole per-emotion effect in one row instead of inferring
// the lid half from firmware source.
//
// READ-ONLY ON PURPOSE, and this is the accurate status, not a shortcut: the lid
// envelopes are `static const LidKey[]` arrays compiled into the T4.1, and the
// firmware serial dispatch (src/main.cpp:581-710) accepts EMOTION:, EYE:, MOUTH:,
// MOUTH_INTENSITY:, MOUTHGEST, EYES:*, IDLE:*, VERSION and SLEEP_CFG: -- there is
// no LID command, so nothing on the Pi can change these at runtime. Making them
// editable needs a firmware change (a LID_CFG: key=value command in the SLEEP_CFG:
// mould, plus a RAM copy of the table) and an operator flash. Scoped in ROADMAP
// RD-069. Keep these strings in step with lidScripts if that table is retuned.
const _LID_EFFECTS = {
  NEUTRAL:     'None, lids follow tracking',
  HAPPY:       'Duchenne squint, lower lid up, 3.5 s',
  CURIOUS:     'Slight widen, blinking held off, 3.7 s',
  ANGRY:       'Upper lid drops, lower tightens, 4.6 s',
  SLEEPY:      'Heavy droop, slow closures, 6.0 s',
  SURPRISED:   'Snaps wide, then settles, 1.2 s',
  SAD:         'Heavy lids, one slow blink, 5.4 s',
  CONFUSED:    'Uncertain drift plus widen, 3.6 s',
  AMUSED:      'Pulsing squint, 2.1 s',
  ANNOYED:     'Narrowed squeeze, quick release, 0.9 s',
  EXASPERATED: 'Eye-roll on a gaze arc, 0.95 s',
};

function _buildEmotionMapUI(data) {
  _emotionMap = data;
  const tbl = document.getElementById('emotion-map-tbl');
  if (!tbl) return;
  tbl.innerHTML = '';
  for (const emo of _EMOTION_NAMES) {
    const curM = data.mouth_map[emo] ?? 0;
    const curE = data.eye_map[emo] ?? -1;
    const eOpts = _EYE_OPT.map(([v,l])=>`<option value="${v}"${v==curE?' selected':''}>${l}</option>`).join('');
    const mOpts = _MOUTH_OPT.map(([v,l])=>`<option value="${v}"${v==curM?' selected':''}>${l}</option>`).join('');
    const lid   = _LID_EFFECTS[emo] || 'None';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="padding:5px 8px;font-size:13px;color:var(--amber);white-space:nowrap">${emo}</td>
      <td style="padding:3px 8px"><select id="em-eye-${emo}">${eOpts}</select></td>
      <td style="padding:3px 8px"><select id="em-mouth-${emo}">${mOpts}</select></td>
      <td style="padding:3px 8px;font-size:12px;color:var(--muted);white-space:nowrap"
          title="Eyelid choreography baked into T4.1 firmware. Not editable from the WebUI -- needs a firmware LID_CFG: command and a flash (ROADMAP RD-069).">${lid}</td>
      <td style="padding:3px 8px"><button class="btn btn-sm" onclick="testEmotionEntry('${emo}')">Test</button></td>`;
    tbl.appendChild(tr);
  }
}

async function loadEmotionMap() {
  _buildEyeGrid();   // S241: same lifecycle as the emotion table (boot + section show)
  try {
    const r = await fetch('/api/emotion_map');
    _buildEmotionMapUI(await r.json());
  } catch(e) { _buildEmotionMapUI({mouth_map:{},eye_map:{}}); }
}

async function saveEmotionMap() {
  const mouthMap={}, eyeMap={};
  for (const emo of _EMOTION_NAMES) {
    const mSel = document.getElementById('em-mouth-'+emo);
    const eSel = document.getElementById('em-eye-'+emo);
    if (mSel) mouthMap[emo] = parseInt(mSel.value);
    if (eSel) eyeMap[emo]   = parseInt(eSel.value);
  }
  const r = await fetch('/api/emotion_map', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({EMOTION_MOUTH_MAP:mouthMap, EMOTION_EYE_MAP:eyeMap})});
  const j = await r.json();
  toast(j.ok ? 'Emotion map saved' : 'Error saving', j.ok);
  if (j.ok) { _emotionMap = {mouth_map:mouthMap, eye_map:eyeMap}; checkSDStatus(); }
}

async function testEmotionEntry(emotion) {
  const eSel = document.getElementById('em-eye-'+emotion);
  const mSel = document.getElementById('em-mouth-'+emotion);
  const eIdx = eSel ? parseInt(eSel.value) : -1;
  const mIdx = mSel ? parseInt(mSel.value) : 0;
  if (eIdx >= 0) await sendTeensy('EYE:'+eIdx);
  await sendTeensy('EMOTION:'+emotion);
  await sendTeensy('MOUTH:'+mIdx);
}

// Uses loaded emotion map if available, falls back to the passed mouthIdx
async function sendEmotion(emotion, fallbackMouthIdx) {
  const eIdx = (_emotionMap.eye_map && emotion in _emotionMap.eye_map) ? _emotionMap.eye_map[emotion] : -1;
  const mIdx = (_emotionMap.mouth_map && emotion in _emotionMap.mouth_map)
    ? _emotionMap.mouth_map[emotion]
    : (fallbackMouthIdx !== undefined ? fallbackMouthIdx : 0);
  if (typeof eIdx === 'number' && eIdx >= 0) await sendTeensy('EYE:'+eIdx);
  await sendTeensy('EMOTION:'+emotion);
  await sendTeensy('MOUTH:'+mIdx);
}

// ── Volume ────────────────────────────────────────────────────────────────────
async function refreshVolume() {
  try {
    const r = await fetch('/api/volume');
    const j = await r.json();
    document.getElementById('vol-slider').value = j.level;
    document.getElementById('vol-display').textContent = `${j.level} (${j.pct}%)`;
  } catch(e) {}
}

async function setVolume() {
  const level = parseInt(document.getElementById('vol-slider').value);
  const r = await fetch('/api/volume', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({level})});
  const j = await r.json();
  if (j.ok) {
    document.getElementById('vol-display').textContent = `${j.level} (${j.pct}%)`;
    toast(`Volume set to ${j.level} (${j.pct}%)`);
  }
}

// ── Bench ──────────────────────────────────────────────────────────────────────
let _benchAutoTimer = null;

function _fmt(v) {
  if (v == null) return '-';
  const n = parseFloat(v);
  return isNaN(n) ? '-' : n.toFixed(2) + 's';
}
function _ts(t) {
  if (!t) return '-';
  try { return new Date(parseFloat(t) * 1000).toLocaleTimeString(); }
  catch(e) { return String(t).slice(0,8); }
}

async function fetchBench() {
  const tbody = document.getElementById('bench-body');
  const cnt   = document.getElementById('bench-count');
  tbody.innerHTML = '<tr><td colspan="15" style="text-align:center;color:var(--muted);padding:16px">Loading...</td></tr>';
  try {
    const r = await fetch('/api/bench');
    const j = await r.json();
    if (j.error) {
      tbody.innerHTML = `<tr><td colspan="15" style="color:var(--red);padding:12px">${j.error}</td></tr>`;
      return;
    }
    const cycles = j.cycles || [];
    cnt.textContent = cycles.length ? cycles.length + ' cycle(s)' : '';
    if (!cycles.length) {
      tbody.innerHTML = '<tr><td colspan="15" style="text-align:center;color:var(--muted);padding:20px">No [BENCH] cycles yet — trigger IRIS to speak first</td></tr>';
    } else {
      tbody.innerHTML = cycles.slice().reverse().map((c, i) => {
        const ls        = c.llm_start || {};
        const tier      = ls.tier || '-';
        const np        = ls.num_predict || '-';
        const rec       = _fmt((c.rec_done || {}).dur_rec);
        const stt       = _fmt((c.stt_done || {}).dur_stt);
        const ttfc      = _fmt((c.llm_first_chunk || {}).dur_ttfc);
        const llm       = _fmt((c.llm_done || {}).dur_llm);
        const tts       = _fmt((c.tts_done || {}).dur_tts);
        const aud       = _fmt((c.audio_done || {}).dur_audio);
        const total     = _fmt((c.audio_done || {}).dur_total);
        const totalRaw  = parseFloat((c.audio_done || {}).dur_total);
        const audRaw    = parseFloat((c.audio_done || {}).dur_audio);
        const ttfwRaw   = (!isNaN(totalRaw) && !isNaN(audRaw)) ? totalRaw - audRaw : NaN;
        const ttfw      = isNaN(ttfwRaw) ? '-' : ttfwRaw.toFixed(2) + 's';
        const ttfwcol   = isNaN(ttfwRaw) ? '' : ttfwRaw < 4 ? 'style="color:var(--green)"' : ttfwRaw < 7 ? 'style="color:var(--amber)"' : 'style="color:var(--red)"';
        const os        = c.ollama_stats || {};
        const ep        = (os.eval_tokens || '-') + '/' + (os.prompt_tokens || '-');
        const snip      = ((c.stt_done || {}).transcript || '').slice(0, 45);
        const n         = totalRaw;
        const tcol      = isNaN(n) ? '' : n < 6 ? 'style="color:var(--green)"' : n < 10 ? 'style="color:var(--amber)"' : 'style="color:var(--red)"';
        return `<tr>
          <td>${cycles.length - i}</td><td>${_ts(c.t)}</td>
          <td>${c.trigger||'?'}</td>
          <td class="tier-${tier}">${tier}</td><td>${np}</td>
          <td>${rec}</td><td>${stt}</td><td>${ttfc}</td><td>${llm}</td><td>${tts}</td><td>${aud}</td>
          <td ${ttfwcol}>${ttfw}</td><td ${tcol}>${total}</td><td>${ep}</td><td title="${((c.stt_done||{}).transcript||'')}">${snip}</td></tr>`;
      }).join('');
    }
    const lev = j.levers || {};
    const levDiv = document.getElementById('bench-levers');
    if (Object.keys(lev).length) {
      const sep = '<span style="color:var(--border);margin:0 2px">|</span>';
      levDiv.innerHTML = [
        'SHORT=<span>' + lev.NUM_PREDICT_SHORT + '</span>',
        'MEDIUM=<span>' + lev.NUM_PREDICT_MEDIUM + '</span>',
        'LONG=<span>' + lev.NUM_PREDICT_LONG + '</span>',
        'MAX=<span>' + lev.NUM_PREDICT_MAX + '</span>',
        'TTS_MAX_CHARS=<span>' + lev.TTS_MAX_CHARS + '</span>',
        'TTS=<span>' + (lev.KOKORO_ENABLED ? 'kokoro' : 'piper') + '</span>',
      ].join(sep);
    } else { levDiv.textContent = 'Could not load config'; }
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="15" style="color:var(--red);padding:12px">Error: ${e}</td></tr>`;
  }
}

function toggleBenchAuto(cb) {
  if (_benchAutoTimer) { clearInterval(_benchAutoTimer); _benchAutoTimer = null; }
  if (cb.checked) _benchAutoTimer = setInterval(fetchBench, 15000);
}

// ── Gesture config ────────────────────────────────────────────────────────────
const _GESTURE_KEYS    = ['VOL+', 'VOL-', 'STOP', 'RIGHT', 'FORWARD', 'BACKWARD', 'CW', 'CCW'];
const _GESTURE_ACTIONS = ['VOL+', 'VOL-', 'STOP', 'LISTEN', 'SLEEP', 'WAKE', 'MUTE', 'SKIP'];
const _GESTURE_LABELS  = {
  'VOL+':    'VOL+ — volume up',
  'VOL-':    'VOL- — volume down',
  'STOP':    'STOP — stop playback',
  'LISTEN':  'LISTEN — trigger listen',
  'SLEEP':   'SLEEP — full sleep sequence',
  'WAKE':    'WAKE — full wake sequence',
  'MUTE':    'MUTE — toggle mute/unmute',
  'SKIP':    'SKIP — do nothing',
};

function _populateGestureSelects() {
  _GESTURE_KEYS.forEach(function(key) {
    const sel = document.getElementById('gesture-' + key);
    if (!sel || sel.options.length > 1) return;
    sel.innerHTML = '';
    _GESTURE_ACTIONS.forEach(function(act) {
      const o = document.createElement('option');
      o.value = act;
      o.textContent = _GESTURE_LABELS[act] || act;
      sel.appendChild(o);
    });
  });
}

async function loadGestureConfig() {
  _populateGestureSelects();
  try {
    const r = await fetch('/api/gesture_config');
    const j = await r.json();
    const map = j.GESTURE_MAP || {};
    _GESTURE_KEYS.forEach(function(key) {
      const sel = document.getElementById('gesture-' + key);
      if (sel && map[key]) sel.value = map[key];
    });
    const es = document.getElementById('gesture-enabled');
    if (es && typeof j.GESTURE_ENABLED !== 'undefined') es.value = j.GESTURE_ENABLED ? 'true' : 'false';
  } catch(e) { toast('Failed to load gesture config', false); }
}

async function saveGestureConfig() {
  const map = {};
  _GESTURE_KEYS.forEach(function(key) {
    const sel = document.getElementById('gesture-' + key);
    if (sel) map[key] = sel.value;
  });
  const es = document.getElementById('gesture-enabled');
  const enabled = es ? es.value === 'true' : true;
  const r = await fetch('/api/gesture_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({GESTURE_MAP: map, GESTURE_ENABLED: enabled})
  });
  const j = await r.json();
  toast(j.ok ? 'Gesture config saved' : 'Error saving gesture config', j.ok);
  if (j.ok) checkSDStatus();
}

// ── Voice barge-in config ─────────────────────────────────────────────────────
// Phrases are fixed (must be in the offline recognizer's lexicon); each maps to
// an action. Mirrors the server _DEFAULT_BARGEIN_GRAMMAR order.
const _BARGEIN_PHRASES = [
  'stop', 'cancel', 'be quiet', 'shut up', 'stop talking', 'pause',
  'louder', 'volume up', 'quieter', 'volume down',
];
const _BARGEIN_ACTIONS = ['STOP', 'VOL+', 'VOL-', 'SKIP'];
const _BARGEIN_ACTION_LABELS = {
  'STOP': 'Stop talking',
  'VOL+': 'Volume up',
  'VOL-': 'Volume down',
  'SKIP': 'Disabled',
};

function _bargeinId(phrase) { return 'bargein-' + phrase.replace(/ /g, '_'); }

function _renderBargeinRows(grammar) {
  const box = document.getElementById('bargein-grammar-rows');
  if (!box) return;
  box.innerHTML = '';
  _BARGEIN_PHRASES.forEach(function(phrase) {
    const row = document.createElement('div');
    row.className = 'field-row';
    const lab = document.createElement('label');
    lab.textContent = '"' + phrase + '"';
    const sel = document.createElement('select');
    sel.id = _bargeinId(phrase);
    _BARGEIN_ACTIONS.forEach(function(act) {
      const o = document.createElement('option');
      o.value = act;
      o.textContent = _BARGEIN_ACTION_LABELS[act];
      sel.appendChild(o);
    });
    sel.value = grammar[phrase] || 'SKIP';
    row.appendChild(lab);
    row.appendChild(sel);
    box.appendChild(row);
  });
}

async function loadBargeinConfig() {
  try {
    const r = await fetch('/api/bargein_config');
    const j = await r.json();
    _renderBargeinRows(j.BARGEIN_GRAMMAR || {});
    const en = document.getElementById('bargein-enabled');
    if (en) en.value = j.BARGEIN_ENABLED ? 'true' : 'false';
    const eng = document.getElementById('bargein-engine');
    if (eng && j.BARGEIN_ENGINE) eng.value = j.BARGEIN_ENGINE;
    // S222: adult detect multiplier (was unreachable from any UI)
    const am = document.getElementById('bargein-mult');
    if (am) am.value = j.BARGEIN_DETECT_MULT ?? 1.5;
    // S199 T3: kids barge-in guard numerics
    const km = document.getElementById('kids-bargein-mult');
    if (km) km.value = j.KIDS_BARGEIN_DETECT_MULT ?? 2.0;
    const kg = document.getElementById('kids-bargein-guard');
    if (kg) kg.value = (j.KIDS_BARGEIN_GUARD_MS ?? 800) / 1000;   // S201b: ms -> seconds
  } catch(e) { toast('Failed to load barge-in config', false); }
}

async function saveBargeinConfig() {
  const grammar = {};
  _BARGEIN_PHRASES.forEach(function(phrase) {
    const sel = document.getElementById(_bargeinId(phrase));
    if (sel && sel.value !== 'SKIP') grammar[phrase] = sel.value;
  });
  const en = document.getElementById('bargein-enabled');
  const eng = document.getElementById('bargein-engine');
  const r = await fetch('/api/bargein_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      BARGEIN_GRAMMAR: grammar,
      BARGEIN_ENABLED: en ? en.value === 'true' : true,
      BARGEIN_ENGINE: eng ? eng.value : 'vosk',
      BARGEIN_DETECT_MULT: parseFloat(document.getElementById('bargein-mult')?.value) || 1.5,
      KIDS_BARGEIN_DETECT_MULT: parseFloat(document.getElementById('kids-bargein-mult')?.value) || 2.0,
      KIDS_BARGEIN_GUARD_MS: Math.round((parseFloat(document.getElementById('kids-bargein-guard')?.value) || 0.8) * 1000),   // S201b: seconds -> ms
    })
  });
  const j = await r.json();
  toast(j.ok ? 'Voice commands saved' : 'Error saving voice commands', j.ok);
}

// ── Gesture log ───────────────────────────────────────────────────────────────
let _gestureLogAutoTimer = null;

async function fetchGestureLog() {
  const box = document.getElementById('gesture-log-events');
  const cnt = document.getElementById('gesture-log-count');
  if (!box) return;
  box.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">Loading...</div>';
  try {
    const r = await fetch('/api/gesture_log');
    const j = await r.json();
    const evs = j.events || [];
    if (cnt) cnt.textContent = evs.length + ' event' + (evs.length !== 1 ? 's' : '');
    if (!evs.length) {
      box.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">No gesture events yet — swipe, push, or rotate over PAJ7620U2 sensor</div>';
      return;
    }
    // Reverse so newest is first in DOM; rAF ensures scrollTop=0 takes effect after paint
    box.innerHTML = evs.slice().reverse().map(e => {
      const dateStr = (e.t || '').slice(0, 10);
      const timeStr = e.ts || '';
      const label   = dateStr ? `${dateStr} ${timeStr}` : timeStr;
      return `<div class="log-event cat-gesture">` +
             `<span class="log-ts" style="width:130px">${_esc(label)}</span>` +
             `<span class="log-cat">[GESTURE]</span>` +
             `<span class="log-msg">${_esc(e.msg || '')}</span>` +
             `</div>`;
    }).join('');
    window.requestAnimationFrame(function() { box.scrollTop = 0; });
  } catch(e) {
    box.innerHTML = `<div style="color:var(--red);padding:12px">Error: ${_esc(String(e))}</div>`;
  }
}

function toggleGestureLogAuto(cb) {
  if (_gestureLogAutoTimer) { clearInterval(_gestureLogAutoTimer); _gestureLogAutoTimer = null; }
  if (cb.checked) _gestureLogAutoTimer = setInterval(fetchGestureLog, 30000);
}

// ── Gesture Activity Monitor (live per-direction hit counts) ───────────────────
let _gestureStatsAutoTimer = null;
let _gestureStatsPrev = {};

async function loadGestureStats() {
  const grid = document.getElementById('gesture-stats-grid');
  const tot  = document.getElementById('gesture-stats-total');
  if (!grid) return;
  try {
    const r = await fetch('/api/gesture_stats');
    const j = await r.json();
    const counts = j.counts || {}, last = j.last || {}, labels = j.labels || {};
    const order  = j.order || Object.keys(counts);
    if (tot) tot.textContent = (j.total || 0) + ' detections in journal';
    grid.innerHTML = order.map(g => {
      const c    = counts[g] || 0;
      const prev = _gestureStatsPrev[g];
      const bumped = (prev !== undefined && c > prev);
      const cls  = c > 0 ? 'gstat hit' : 'gstat zero';
      const seen = last[g] ? ('last ' + last[g]) : 'never seen';
      return `<div class="gstat ${cls}${bumped ? ' flash' : ''}">` +
             `<div class="gstat-dir">${_esc(labels[g] || g)}<span class="gstat-raw">${_esc(g)}</span></div>` +
             `<div class="gstat-count">${c}</div>` +
             `<div class="gstat-last">${_esc(seen)}</div>` +
             `</div>`;
    }).join('');
    _gestureStatsPrev = Object.assign({}, counts);
    if (grid.querySelector('.flash')) {
      setTimeout(() => grid.querySelectorAll('.flash').forEach(e => e.classList.remove('flash')), 700);
    }
  } catch(e) {
    grid.innerHTML = `<div style="color:var(--red);padding:12px;grid-column:1/-1">Error: ${_esc(String(e))}</div>`;
  }
}

function toggleGestureStatsAuto(cb) {
  if (_gestureStatsAutoTimer) { clearInterval(_gestureStatsAutoTimer); _gestureStatsAutoTimer = null; }
  if (cb.checked) { loadGestureStats(); _gestureStatsAutoTimer = setInterval(loadGestureStats, 3000); }
}

// ── Person Sensor live status (T4.1 eye-tracking sensor) ───────────────────────
let _psStatusAutoTimer = null;

async function loadPsStatus() {
  const dot = document.getElementById('ps-status-dot');
  const lab = document.getElementById('ps-status-label');
  const st  = document.getElementById('ps-status-stats');
  const box = document.getElementById('ps-status-events');
  if (!dot) return;
  try {
    const r = await fetch('/api/ps/status');
    const j = await r.json();
    const state = j.state || 'unknown';
    dot.className = 'ps-dot ps-dot-' + state;
    if (lab) lab.textContent = j.label || state;
    if (st) {
      st.innerHTML =
        `<span class="ps-chip">Acquisitions: <b>${j.acquisitions || 0}</b></span>` +
        `<span class="ps-chip">Last lock: <b>${_esc((j.last_face1 || '—').slice(11,19) || '—')}</b></span>` +
        `<span class="ps-chip">Last lost: <b>${_esc((j.last_face0 || '—').slice(11,19) || '—')}</b></span>` +
        (j.last_absent ? `<span class="ps-chip">Last no-ACK: <b>${_esc(j.last_absent.slice(11,19))}</b></span>` : '') +
        (j.last_heartbeat ? `<span class="ps-chip">PS heartbeat: <b>${_esc(j.last_heartbeat.slice(11,19))}</b></span>` : '') +
        (j.last_link ? `<span class="ps-chip">Serial link opened: <b>${_esc(j.last_link.slice(11,19))}</b></span>` : '');
    }
    if (box) {
      const evs = j.recent || [];
      if (!evs.length) {
        box.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">No lock/lose transitions in journal yet (the bridge heartbeat above still confirms the link is live)</div>';
      } else {
        const col = {track:'var(--green)', lost:'var(--blue)', detected:'var(--green)', absent:'var(--red)'};
        box.innerHTML = evs.map(e =>
          `<div class="log-event"><span class="log-ts" style="width:80px">${_esc(e.ts || '')}</span>` +
          `<span class="log-msg" style="color:${col[e.kind] || 'var(--text)'}">${_esc(e.msg || '')}</span></div>`
        ).join('');
      }
    }
  } catch(e) {
    if (lab) lab.textContent = 'Error: ' + String(e);
  }
}

function togglePsStatusAuto(cb) {
  if (_psStatusAutoTimer) { clearInterval(_psStatusAutoTimer); _psStatusAutoTimer = null; }
  if (cb.checked) { loadPsStatus(); _psStatusAutoTimer = setInterval(loadPsStatus, 5000); }
}

// ── Person Sensor LED indicators (liveness) ────────────────────────────────────
function _renderEyesLedAck(ack) {
  const span = document.getElementById('ps-led-eyes-ack');
  if (!span) return;
  const a = ack && ack.LED;
  span.textContent = a ? `Teensy confirmed ${a.value === '1' ? 'ON' : 'off'} @ ${a.ts}`
                        : 'not yet confirmed by Teensy';
}

async function loadSensorLeds() {
  try { const j = await (await fetch('/api/ps/config')).json();
        const e = document.getElementById('ps-led-eyes'); if (e) e.checked = !!j.LED;
        _renderEyesLedAck(j.ack); } catch(e) {}
  try { const j = await (await fetch('/api/servo/config')).json();
        const s = document.getElementById('ps-led-servo'); if (s) s.checked = !!j.LED; } catch(e) {}
}

async function setEyesLed(on) {
  try {
    await fetch('/api/ps/config', {method:'POST', headers:{'Content-Type':'application/json'},
                                   body: JSON.stringify({LED: on ? 1 : 0})});
    await fetch('/api/ps/config/persist', {method:'POST'});
    const j = await (await fetch('/api/ps/config')).json();
    _renderEyesLedAck(j.ack);
    toast('Eyes (T4.1) sensor LED ' + (on ? 'ON' : 'off') + ' sent — see confirmation below', true);
  } catch(e) { toast('Eyes LED failed: ' + e, false); }
}

async function setServoLed(on) {
  try {
    const j = await (await fetch('/api/servo/led', {method:'POST',
                  headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({LED: on ? 1 : 0})})).json();
    toast('Servo (T4.0) sensor LED ' + (on ? 'ON' : 'off') +
          (j.sent ? ' — needs S150d firmware to light' : ' (bridge not listening yet)'), !!j.ok);
  } catch(e) { toast('Servo LED failed: ' + e, false); }
}

// ── POST diagnostic ───────────────────────────────────────────────────────────
let _postPollTimer = null;

const _POST_STATUS_COLORS = {
  PASS: 'var(--green)', WARN: 'var(--amber)', FAIL: 'var(--red)',
  SKIP: 'var(--muted)', ERROR: 'var(--red)'
};

async function runPost() {
  const btn = document.getElementById('btn-post');
  const statusEl = document.getElementById('post-status');
  const resultEl = document.getElementById('post-result');
  btn.disabled = true;
  statusEl.textContent = 'starting...';
  statusEl.style.color = 'var(--blue)';
  resultEl.style.display = 'none';
  try {
    const r = await fetch('/api/post', {method: 'POST'});
    const j = await r.json();
    if (!j.ok && j.error) {
      statusEl.textContent = j.error;
      statusEl.style.color = 'var(--red)';
      btn.disabled = false;
      return;
    }
  } catch(e) {
    statusEl.textContent = 'request failed';
    statusEl.style.color = 'var(--red)';
    btn.disabled = false;
    return;
  }
  statusEl.textContent = 'running...';
  if (_postPollTimer) clearInterval(_postPollTimer);
  _postPollTimer = setInterval(_pollPost, 2000);
}

async function _pollPost() {
  const btn = document.getElementById('btn-post');
  const statusEl = document.getElementById('post-status');
  try {
    const r = await fetch('/api/post');
    const j = await r.json();
    if (j.running) { statusEl.textContent = 'running...'; return; }
    clearInterval(_postPollTimer); _postPollTimer = null;
    btn.disabled = false;
    _renderPostResult(j.result);
  } catch(e) {
    statusEl.textContent = 'poll error';
  }
}

function _renderPostResult(result) {
  if (!result) return;
  const statusEl  = document.getElementById('post-status');
  const resultEl  = document.getElementById('post-result');
  const verdictEl = document.getElementById('post-verdict');
  const rowsEl    = document.getElementById('post-rows');

  const ok = result.verdict === 'AUTHORIZED';
  statusEl.textContent = `done — ${result.ts || ''}`;
  statusEl.style.color = ok ? 'var(--green)' : 'var(--red)';

  const vColor = ok ? 'var(--green)' : 'var(--red)';
  verdictEl.innerHTML =
    `<span style="color:${vColor}">${_esc(result.verdict)}</span>` +
    `&nbsp; ${result.n_pass}/${result.n_total} PASS` +
    (result.n_warn ? `&nbsp; <span style="color:var(--amber)">${result.n_warn} WARN</span>` : '') +
    (result.n_fail ? `&nbsp; <span style="color:var(--red)">${result.n_fail} FAIL</span>` : '');

  rowsEl.innerHTML = (result.checks || []).map(c => {
    const col = _POST_STATUS_COLORS[c.status] || 'var(--muted)';
    return `<tr>
      <td style="text-align:left;color:var(--muted)">${_esc(c.layer)}</td>
      <td style="text-align:left">${_esc(c.check)}</td>
      <td style="text-align:left;color:${col};font-weight:700">${_esc(c.status)}</td>
      <td style="text-align:left;color:var(--muted);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(c.detail || '')}</td>
    </tr>`;
  }).join('');

  resultEl.style.display = 'block';
}

// --- Vision Cal Tab (Person Sensor calibration) ---
let _oglecalInited = false;

function _oglecalTabHook() {
  if (!_oglecalInited) {
    _oglecalInited = true;
    _buildPsCfgFields(null);
    loadPsConfig();
  }
}

// ── Person Sensor Config (T4.1 Person Sensor — PS_CFG serial, S141) ───────────
const _PS_CFG_FIELDS = [
  // [key, label, type, min, max, step, default]
  ['CONF',    'Confidence gate (0–100)',     'range',  0,    100,   5,    60   ],
  // S212: SEN0626 has no is_facing equivalent, so the shim hardcodes is_facing=1
  // and this gate can never reject anything. Labelled rather than removed because
  // the I2C Person Sensor rollback (USE_PERSON_SENSOR_I2C) restores a real bit.
  ['FACING',  'Require facing camera (no effect on SEN0626)', 'toggle', 0, 1,  1,    1    ],
  ['LOST_MS', 'Face-lost timeout (s)',            'range',  1,    15,    0.5,  5    ],
  ['Y_BIAS',  'Y bias (neg = look up)',           'range',  -1.0, 1.0,   0.05, 0.0  ],
  // S212c gaze shaping: targetN = rawN * gain + bias. Gain is SIGNED - the sign is
  // the direction (flip it if the eyes track mirrored) and the magnitude is the
  // range. At a close conversational distance a head only crosses a fraction of the
  // sensor's 85 deg FOV, so raw deflection is small and 1.0 reads as "barely moves";
  // 2.0-2.5 makes it read as real gaze. +1.0 is the SEN0626 convention; a rollback to
  // the I2C Person Sensor needs X_GAIN=-1.0.
  ['X_GAIN',  'X gain (neg = mirror L/R)',        'range',  -3.0, 3.0,   0.1,  1.0  ],
  ['Y_GAIN',  'Y gain (neg = mirror U/D)',        'range',  -3.0, 3.0,   0.1,  1.0  ],
  ['X_BIAS',  'X bias (neg = look left)',         'range',  -1.0, 1.0,   0.05, 0.0  ],
];

function _buildPsCfgFields(data) {
  const container = document.getElementById('ps-cfg-fields');
  if (!container) return;
  container.innerHTML = '';
  _PS_CFG_FIELDS.forEach(([key, label, type, min, max, step, def]) => {
    // S201b: data[key] is the wire value (LOST_MS in ms); def is already display units (s).
    const val = (data && data[key] != null) ? _psToDisp(key, parseFloat(data[key])) : parseFloat(def);
    const row = document.createElement('div');
    row.className = 'field-row';
    if (type === 'toggle') {
      const isOn = (val == 1);
      row.innerHTML =
        `<label style="flex:1">${label}</label>` +
        `<input type="checkbox" id="psf-${key}" ${isOn ? 'checked' : ''} ` +
        `style="width:18px;height:18px;cursor:pointer" ` +
        `onchange="document.getElementById('psf-v-${key}').textContent=this.checked?'1':'0'">` +
        `<span id="psf-v-${key}" style="width:28px;color:var(--text);font-size:13px;flex-shrink:0;text-align:right">${isOn ? '1' : '0'}</span>`;
    } else {
      const dispVal = step < 0.1 ? val.toFixed(3) : step < 1 ? val.toFixed(2) : '' + Math.round(val);
      row.innerHTML =
        `<label style="min-width:210px">${label}</label>` +
        `<input type="range" id="psf-${key}" min="${min}" max="${max}" step="${step}" value="${val}" ` +
        `style="flex:1;accent-color:var(--blue);height:6px;cursor:pointer" ` +
        `oninput="_psfUpdate('${key}',this.value,${step})">` +
        `<span id="psf-v-${key}" style="width:52px;color:var(--text);font-size:13px;flex-shrink:0;text-align:right">${dispVal}</span>`;
    }
    container.appendChild(row);
  });
}

function _psfUpdate(key, rawVal, step) {
  const sp = document.getElementById('psf-v-' + key);
  if (!sp) return;
  const n = parseFloat(rawVal);
  sp.textContent = step < 0.1 ? n.toFixed(3) : step < 1 ? n.toFixed(2) : '' + Math.round(n);
}

function resetPsConfigDefaults() {
  _PS_CFG_FIELDS.forEach(([key, , type, , , step, def]) => {
    const el = document.getElementById('psf-' + key);
    const vEl = document.getElementById('psf-v-' + key);
    if (!el) return;
    if (type === 'toggle') {
      el.checked = (def == 1);
      if (vEl) vEl.textContent = def == 1 ? '1' : '0';
    } else {
      el.value = def;
      if (vEl) {
        const val = parseFloat(def);
        vEl.textContent = step < 0.1 ? val.toFixed(3) : step < 1 ? val.toFixed(2) : '' + Math.round(val);
      }
    }
  });
}

async function loadPsConfig() {
  const msg = document.getElementById('ps-cfg-msg');
  if (msg) { msg.textContent = 'loading...'; msg.style.color = 'var(--muted)'; }
  try {
    const r = await fetch('/api/ps/config');
    const data = await r.json();
    _PS_CFG_FIELDS.forEach(([key, label, type, min, max, step]) => {
      if (data[key] == null) return;
      const el = document.getElementById('psf-' + key);
      const sp = document.getElementById('psf-v-' + key);
      if (!el) return;
      if (type === 'toggle') {
        el.checked = (parseFloat(data[key]) == 1);
        if (sp) sp.textContent = el.checked ? '1' : '0';
      } else {
        const disp = _psToDisp(key, parseFloat(data[key]));   // S201b: wire ms -> display s for LOST_MS
        el.value = disp;
        if (sp) {
          sp.textContent = step < 0.1 ? disp.toFixed(3) : step < 1 ? disp.toFixed(2) : '' + Math.round(disp);
        }
      }
    });
    if (msg) { msg.textContent = 'loaded'; msg.style.color = 'var(--muted)'; }
  } catch(e) {
    if (msg) { msg.textContent = 'load failed: ' + e; msg.style.color = 'var(--red)'; }
  }
}

async function savePsConfig() {
  const msg = document.getElementById('ps-cfg-msg');
  if (msg) { msg.textContent = 'saving...'; msg.style.color = 'var(--muted)'; }
  const body = {};
  _PS_CFG_FIELDS.forEach(([key, label, type]) => {
    const el = document.getElementById('psf-' + key);
    if (!el) return;
    body[key] = type === 'toggle' ? (el.checked ? 1 : 0) : _psToWire(key, parseFloat(el.value));   // S201b: display s -> wire ms for LOST_MS
  });
  try {
    const r = await fetch('/api/ps/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.ok) {
      if (msg) { msg.textContent = 'saved — sent to Teensy'; msg.style.color = 'var(--green)'; }
      toast('Person Sensor config applied');
    } else {
      if (msg) { msg.textContent = 'failed: ' + (j.error || 'unknown'); msg.style.color = 'var(--red)'; }
      toast('PS config save failed: ' + (j.error || 'error'), false);
    }
  } catch(e) {
    if (msg) { msg.textContent = 'error: ' + e; msg.style.color = 'var(--red)'; }
    toast('PS config save error', false);
  }
}

async function persistPsConfig() {
  const msg = document.getElementById('ps-cfg-msg');
  if (msg) { msg.textContent = 'persisting...'; msg.style.color = 'var(--muted)'; }
  try {
    const r = await fetch('/api/ps/config/persist', {method: 'POST'});
    const j = await r.json();
    if (j.ok) {
      if (msg) { msg.textContent = `persisted to SD — md5: ${j.md5 || '?'}`; msg.style.color = 'var(--green)'; }
      toast('Person Sensor config persisted to SD', true, 4000);
    } else {
      if (msg) { msg.textContent = 'persist failed: ' + (j.error || 'unknown'); msg.style.color = 'var(--red)'; }
      toast('Persist failed (save first?)', false);
    }
  } catch(e) {
    if (msg) { msg.textContent = 'error: ' + e; msg.style.color = 'var(--red)'; }
    toast('Persist error', false);
  }
}

// ── Turn Latency / Bench Recent (RD-007 S158) ─────────────────────────────────
async function fetchBenchRecent() {
  let j;
  try { j = await (await fetch('/api/bench_recent')).json(); }
  catch (e) { return; }
  const entries = j.entries || [];
  _drawSpark('lt-spark', entries.map(r => r.total_ms).filter(v => v != null));
  const bd = document.getElementById('lt-breakdown');
  if (!bd) return;
  if (!entries.length) { bd.textContent = 'No bench data yet.'; return; }
  const last = entries[entries.length - 1];
  const fmt = (label, val) => val != null ? `${label} ${val} ms` : `${label} —`;
  const parts = [fmt('STT', last.stt_ms), fmt('LLM', last.llm_ms), fmt('TTS', last.tts_ms)];
  const tot = last.total_ms != null ? `  ·  total ${last.total_ms} ms` : '';
  const cold = last.cold ? ' (cold start)' : '';
  bd.textContent = parts.join(' / ') + tot + cold;
}


// ── Field definitions: click popover (S222) ───────────────────────────────────
// The text comes from CFG_MAP in iris_defs.js, the SAME array the help page's
// settings table renders. There is no second copy to drift. Row shape:
//   [section, key, type, range, default, tab, description, behavior?]
// The optional 8th element is the tier: present means a real "what this changes
// in her behavior" line for a knob humans turn; absent means the popover is
// generated from the columns, which is correct for plumbing.
//
// Click, not hover: the WebUI is used from a phone and hover does not exist on
// touch. Badges are injected from the data, so a new row needs no HTML edit.

// Controls whose element id is not the config key (verified S222).
const _DEF_ALIAS = {
  'vol-slider': 'SPEAKER_VOLUME',
  'default-eye-sel': 'DEFAULT_EYE_IDX',
  'kids-bargein-mult': 'KIDS_BARGEIN_DETECT_MULT',
};

function _defRow(key) {
  if (typeof CFG_MAP === 'undefined') return null;
  return CFG_MAP.find(r => r[1] === key) || null;
}

function showDef(key) {
  const r = _defRow(key);
  if (!r) return;
  document.getElementById('defpop-title').textContent = r[0] + ': ' + r[1];
  // Tier 1 leads with behavior; the reference description follows it.
  document.getElementById('defpop-body').textContent = r[7] ? r[7] : (r[6] || '');
  const meta = [];
  if (r[2] && r[2] !== 'n/a') meta.push(r[2]);
  if (r[3] && r[3] !== 'n/a') meta.push('range ' + r[3]);
  if (r[4] && r[4] !== 'n/a') meta.push('default ' + r[4]);
  if (r[7] && r[6]) meta.push('—');
  document.getElementById('defpop-meta').textContent =
    meta.filter(x => x !== '—').join('  ·  ') + (r[7] && r[6] ? '\n' + r[6] : '');
  document.getElementById('defpop').style.display = 'block';
}

function closeDef() { document.getElementById('defpop').style.display = 'none'; }

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDef(); });

// Inject one badge per field that has a definition. Idempotent, so it is safe
// to re-run after a tab builds its controls dynamically.
function attachDefBadges() {
  if (typeof CFG_MAP === 'undefined') return;
  const byKey = {};
  CFG_MAP.forEach(r => { byKey[r[1]] = r; });
  const targets = [];
  Object.keys(byKey).forEach(k => targets.push([k, k]));
  Object.entries(_DEF_ALIAS).forEach(([id, k]) => { if (byKey[k]) targets.push([id, k]); });

  targets.forEach(([id, key]) => {
    const el = document.getElementById(id);
    if (!el) return;
    const row = el.closest('.field-row') || el.parentElement;
    if (!row || row.querySelector('.def-badge[data-k="' + key + '"]')) return;
    const host = row.querySelector('label') || row;
    const b = document.createElement('span');
    b.className = 'def-badge';
    b.dataset.k = key;
    b.textContent = '?';
    b.title = 'What does this do?';
    b.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;' +
      'width:15px;height:15px;margin-left:6px;border-radius:50%;cursor:pointer;' +
      'border:1px solid var(--border);color:var(--muted);font-size:10px;' +
      'font-weight:700;vertical-align:middle;flex-shrink:0';
    b.onclick = (e) => { e.stopPropagation(); showDef(key); };
    host.appendChild(b);
  });
}

// ── Init ───────────────────────────────────────────────────────────────────────
loadConfig();
loadEmotionMap();
pollStatus();
pollSleepState();
checkSDStatus();
pollSysstat();
attachDefBadges();
pollVersion();
fetchBenchRecent();
setInterval(pollStatus, 15000);
setInterval(pollSleepState, 5000);
setInterval(checkSDStatus, 30000);
setInterval(pollSysstat, 10000);
setInterval(pollVersion, 60000);
setInterval(fetchBenchRecent, 30000);

// ── Kids tab (RD-047) ─────────────────────────────────────────────────────────
// Kids mode was voice-entry only and reverted silently after 30 min of quiet.
// The toggle reads the LIVE mode from /api/kids_mode (backed by the flag file
// assistant.py maintains) so it never lies about the current state.

function _kidsBoolSel(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  const on = (val === true || val === 1 || val === '1' || val === 'true');
  el.value = on ? '1' : '0';
}

async function loadKidsMode() {
  try {
    const r = await fetch('/api/kids_mode');
    const j = await r.json();
    _kidsBoolSel('kids-mode-sel', j.enabled);
    const s = document.getElementById('kids-mode-state');
    if (s) {
      s.textContent = j.enabled ? 'live: ON' : 'live: off';
      s.style.color = j.enabled ? 'var(--accent)' : '';
    }
  } catch (e) { /* assistant may be restarting */ }
}

async function saveKidsMode() {
  const el = document.getElementById('kids-mode-sel');
  if (!el) return;
  const on = el.value === '1';
  const r = await fetch('/api/kids_mode', {method:'POST', headers:{'Content-Type':'application/json'},
                                           body: JSON.stringify({enabled: on})});
  const j = await r.json();
  toast(j.ok ? (on ? 'Kids mode ON' : 'Kids mode off') : 'Toggle failed', j.ok);
  setTimeout(loadKidsMode, 400);   // read back the live flag
}

let _kidsProfile = {children: []};

function _renderKidRows() {
  const box = document.getElementById('kids-profile-rows');
  if (!box) return;
  box.innerHTML = '';
  // Build with real nodes + addEventListener rather than inline oninput= strings:
  // a child's name or interest can contain quotes, and an escaped newline inside a
  // stringly-built handler attribute is a browser SyntaxError waiting to happen.
  _kidsProfile.children.forEach((c, i) => {
    const card = document.createElement('div');
    card.className = 'card';
    card.style.margin = '8px 0';

    const mkRow = (labelHtml, field) => {
      const row = document.createElement('div');
      row.className = 'field-row';
      const lab = document.createElement('label');
      lab.innerHTML = labelHtml;
      row.appendChild(lab);
      row.appendChild(field);
      card.appendChild(row);
      return row;
    };

    const name = document.createElement('input');
    name.type = 'text';
    name.value = c.name || '';
    name.addEventListener('input', () => { _kidsProfile.children[i].name = name.value; });
    mkRow('Name', name);

    const age = document.createElement('input');
    age.type = 'number'; age.min = '0'; age.max = '21'; age.style.width = '80px';
    age.value = (c.age === null || c.age === undefined) ? '' : c.age;
    age.addEventListener('input', () => {
      _kidsProfile.children[i].age = age.value === '' ? null : Number(age.value);
    });
    mkRow('Age', age);

    const ta = document.createElement('textarea');
    ta.rows = 5; ta.style.flex = '1'; ta.style.minWidth = '260px';
    ta.value = (c.interests || []).join('\n');
    ta.addEventListener('input', () => {
      _kidsProfile.children[i].interests =
        ta.value.split('\n').map(s => s.trim()).filter(Boolean);
    });
    mkRow('Interests<br><span class="hint">one per line</span>', ta).style.alignItems = 'flex-start';

    const del = document.createElement('button');
    del.className = 'btn';
    del.textContent = 'Remove ' + (c.name || 'child');
    del.addEventListener('click', () => removeKidRow(i));
    card.appendChild(del);

    box.appendChild(card);
  });
}

function addKidRow() {
  _kidsProfile.children.push({name: '', age: null, interests: []});
  _renderKidRows();
}

function removeKidRow(i) {
  _kidsProfile.children.splice(i, 1);
  _renderKidRows();
}

async function loadKidsProfile() {
  try {
    const r = await fetch('/api/kids_profile');
    const j = await r.json();
    _kidsProfile = {children: j.children || []};
    _renderKidRows();
  } catch (e) { toast('Profile load failed', false); }
}

async function saveKidsProfile() {
  const r = await fetch('/api/kids_profile', {method:'POST', headers:{'Content-Type':'application/json'},
                                              body: JSON.stringify({children: _kidsProfile.children})});
  const j = await r.json();
  if (!j.ok) { toast('Save failed: ' + (j.error || 'error'), false, 4000); return; }
  _kidsProfile = {children: j.children || []};
  _renderKidRows();
  toast(j.sd === false ? 'Saved to RAM (SD persist FAILED)' : 'Profile saved — RAM + SD', j.sd !== false, 3500);
}

function _kidsTabHook() {
  loadKidsMode();
  loadKidsProfile();
  _kidsBoolSel('KIDS_ENDPOINT_CUE', _cfg.KIDS_ENDPOINT_CUE);
  _kidsBoolSel('KIDS_GAP_FILLERS',  _cfg.KIDS_GAP_FILLERS);
}
