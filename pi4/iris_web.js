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
  if (name === 'logs') fetchLogs();
  if (name === 'audio') refreshVolume();
  if (name === 'system') { pollStatus(); checkSDStatus(); }
  if (name === 'soundboard') fetchSoundboard();
  if (name === 'voice') { loadKokoroVoices(); }
  if (name === 'gandalf') loadVram();
  if (name === 'bench') { fetchBench(); fetchBenchRecent(); }
  if (name === 'gestures') { loadGestureConfig(); fetchGestureLog(); loadGestureStats(); }
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
async function loadConfig() {
  const r = await fetch('/api/config');
  _cfg = await r.json();
  for (const [k, v] of Object.entries(_cfg)) {
    const el = document.getElementById(k);
    if (!el) continue;
    if (el.tagName === 'SELECT') el.value = String(v);
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
}

async function saveFields(keys) {
  const patch = {};
  for (const k of keys) {
    const el = document.getElementById(k);
    if (!el) continue;
    const raw = el.value;
    patch[k] = isNaN(raw) || raw === '' ? raw : Number(raw);
  }
  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)});
  const j = await r.json();
  toast(j.ok ? 'Saved to RAM' : 'Error', j.ok);
  if (j.ok) checkSDStatus();
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
  document.getElementById('lbl-temp').textContent = j.cpu_temp + 'C';
  document.getElementById('lbl-uptime').textContent = j.uptime;
  dot.className = 'dot' + (j.running ? ' on' : '');
  lbl.textContent = j.running ? 'running' : 'stopped';
  const sr = document.getElementById('sys-running');
  const st = document.getElementById('sys-temp');
  const su = document.getElementById('sys-uptime');
  if(sr) { sr.textContent = j.running ? 'running' : 'stopped'; sr.style.color = j.running ? 'var(--green)' : 'var(--red)'; }
  if(st) st.textContent = j.cpu_temp + 'C';
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
  set('ss-temp',    (j.temp_c != null ? j.temp_c + 'C' : '?'),
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
  try {
    const r = await fetch('/api/vram');
    const j = await r.json();
    if (j.error) { box.textContent = 'Gandalf offline: ' + j.error; return; }
    const models = j.models || [];
    if (!models.length) { box.textContent = 'No models loaded in VRAM'; return; }
    box.textContent = models.map(m =>
      `${m.name}\n  size: ${(m.size/1e9).toFixed(1)} GB  vram: ${(m.size_vram/1e9).toFixed(1)} GB`
    ).join('\n\n');
  } catch(e) { box.textContent = 'Error: ' + e; }
}

// ── Chat ───────────────────────────────────────────────────────────────────────
let _chatMode    = 'silent';   // 'silent' | 'speak' | 'verbatim'
let _chatPersona = 'adult';

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
      body: JSON.stringify({text, speak: _chatMode === 'speak', mode: _chatPersona})});
    const j = await r.json();
    if (j.reply) {
      const spokenTag  = j.spoken  ? ' [spoken]'           : '';
      const emotionTag = j.emotion ? ` {${j.emotion}}`     : '';
      thinking.textContent = 'IRIS' + spokenTag + emotionTag + ': ' + j.reply;
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
const _EMOTION_NAMES = ['NEUTRAL','HAPPY','CURIOUS','ANGRY','SLEEPY','SURPRISED','SAD','CONFUSED','AMUSED'];
const _EYE_OPT = [[-1,'Default (auto)'],[0,'0 - Nordic Blue'],[1,'1 - Flame'],[2,'2 - Hypno Red'],
  [3,'3 - Hazel'],[4,'4 - Blue Flame 1'],[5,'5 - Dragon'],[6,'6 - Striking Blue']];
const _MOUTH_OPT = [[0,'0 - Neutral'],[1,'1 - Happy'],[2,'2 - Curious'],[3,'3 - Angry'],
  [4,'4 - Sleepy'],[5,'5 - Surprised'],[6,'6 - Sad'],[7,'7 - Confused'],
  [8,'8 - Sleep'],[9,'9 - Silly (tongue)']];

let _emotionMap = {mouth_map:{}, eye_map:{}};

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
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="padding:5px 8px;font-size:13px;color:var(--amber);white-space:nowrap">${emo}</td>
      <td style="padding:3px 8px"><select id="em-eye-${emo}">${eOpts}</select></td>
      <td style="padding:3px 8px"><select id="em-mouth-${emo}">${mOpts}</select></td>
      <td style="padding:3px 8px"><button class="btn btn-sm" onclick="testEmotionEntry('${emo}')">Test</button></td>`;
    tbl.appendChild(tr);
  }
}

async function loadEmotionMap() {
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
  } catch(e) { toast('Failed to load gesture config', false); }
}

async function saveGestureConfig() {
  const map = {};
  _GESTURE_KEYS.forEach(function(key) {
    const sel = document.getElementById('gesture-' + key);
    if (sel) map[key] = sel.value;
  });
  const r = await fetch('/api/gesture_config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({GESTURE_MAP: map})
  });
  const j = await r.json();
  toast(j.ok ? 'Gesture config saved' : 'Error saving gesture config', j.ok);
  if (j.ok) checkSDStatus();
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
  ['FACING',  'Require facing camera',            'toggle', 0,    1,     1,    1    ],
  ['LOST_MS', 'Face-lost timeout (ms)',           'range',  1000, 15000, 500,  5000 ],
  ['Y_BIAS',  'Y bias (neg = look up)',           'range',  -1.0, 1.0,   0.05, 0.0  ],
];

function _buildPsCfgFields(data) {
  const container = document.getElementById('ps-cfg-fields');
  if (!container) return;
  container.innerHTML = '';
  _PS_CFG_FIELDS.forEach(([key, label, type, min, max, step, def]) => {
    const rawVal = (data && data[key] != null) ? data[key] : def;
    const val = parseFloat(rawVal);
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
        el.value = data[key];
        if (sp) {
          const n = parseFloat(data[key]);
          sp.textContent = step < 0.1 ? n.toFixed(3) : step < 1 ? n.toFixed(2) : '' + Math.round(n);
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
    body[key] = type === 'toggle' ? (el.checked ? 1 : 0) : parseFloat(el.value);
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

// ── Init ───────────────────────────────────────────────────────────────────────
loadConfig();
loadEmotionMap();
pollStatus();
pollSleepState();
checkSDStatus();
pollSysstat();
fetchBenchRecent();
setInterval(pollStatus, 15000);
setInterval(pollSleepState, 5000);
setInterval(checkSDStatus, 30000);
setInterval(pollSysstat, 10000);
setInterval(fetchBenchRecent, 30000);
