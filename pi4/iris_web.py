#!/usr/bin/env python3
"""IRIS Web Config Panel — Flask server (Pi4)."""
import json, os, re, subprocess, time, wave, tempfile, threading
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
import sys; sys.path.insert(0, "/home/pi")
from core.config import CMD_PORT
from log_parser import _TS_RE, _MSG_RE, _DRIFT_SIGNALS, _parse_event_msg, _sd_events

app = Flask(__name__)
CORS(app, origins=["http://localhost:8080", "http://127.0.0.1:8080"])

# Soundboard quip/clip manager (S163) — self-contained blueprint; a load failure
# must never take down the rest of the WebUI.
try:
    from soundboard_api import soundboard_bp
    app.register_blueprint(soundboard_bp)
except Exception as _sb_e:
    print(f"[WEB] soundboard blueprint not loaded: {_sb_e}", flush=True)

GANDALF      = "192.168.1.3"
OLLAMA_PORT  = 11434
KOKORO_URL   = "http://192.168.1.3:8004"
OGLE_URL     = "http://192.168.1.202"   # OGLE vision node management plane (RD-033)
CONFIG_FILE  = "/home/pi/iris_config.json"
SD_CONFIG    = "/media/root-ro/home/pi/iris_config.json"
_WEB_DIR     = os.path.dirname(os.path.abspath(__file__))
HTML_FILE    = os.path.join(_WEB_DIR, "iris_web.html")
CSS_FILE     = os.path.join(_WEB_DIR, "iris_web.css")
JS_FILE      = os.path.join(_WEB_DIR, "iris_web.js")
HELP_FILE    = os.path.join(_WEB_DIR, "iris_help.html")
SLEEP_FLAG   = "/tmp/iris_sleep_mode"
_cfg_lock    = threading.Lock()  # RD-034: serialise all config read-modify-write + persist

# ── helpers ────────────────────────────────────────────────────────────────────
def read_cfg():
    try:
        with open(CONFIG_FILE) as f: return json.load(f)
    except Exception: return {}

def write_cfg(patch):
    import shutil as _sh
    with _cfg_lock:
        cfg = read_cfg()
        # Auto-heal: if config is 0-byte but goldbak is valid, restore silently before writing.
        if not cfg and os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) == 0:
            _gbak = CONFIG_FILE + ".goldbak"
            try:
                _gc = json.load(open(_gbak))
                if _gc:
                    _sh.copy2(_gbak, CONFIG_FILE)
                    cfg = _gc
                    print(f"[CFG] auto-healed from goldbak ({len(cfg)} keys)")
            except Exception:
                pass
        if not cfg and os.path.exists(CONFIG_FILE) and os.path.getsize(CONFIG_FILE) == 0:
            raise RuntimeError("iris_config.json is empty/unreadable -- restore it first")
        cfg.update(patch)
        if not cfg:
            raise RuntimeError("refusing to write an empty config")
        _tmp = CONFIG_FILE + f".tmp.{threading.get_ident()}"
        try: _sh.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
        except Exception: pass
        blob = json.dumps(cfg, indent=2)
        with open(_tmp, "w") as f: f.write(blob)
        if os.path.getsize(_tmp) < 3:
            try: os.unlink(_tmp)
            except Exception: pass
            raise RuntimeError("tmp config wrote <3 bytes -- aborted, original preserved")
        os.replace(_tmp, CONFIG_FILE)
        try: _sh.copy2(CONFIG_FILE, CONFIG_FILE + ".goldbak")
        except Exception: pass

def cpu_temp():
    try: return round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000.0, 1)
    except Exception: return 0.0

def uptime_str():
    try:
        s = float(open("/proc/uptime").read().split()[0])
        return f"{int(s//3600)}h {int((s%3600)//60)}m"
    except Exception: return "?"

def send_teensy(cmd):
    """Forward command to assistant.py via UDP -- it owns the serial port."""
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.sendto((cmd.strip() + "\n").encode(), ("127.0.0.1", CMD_PORT))
        return True
    except Exception as e:
        print(f"[WEB] Teensy UDP: {e}"); return False

def _sd_synced():
    """Return True if iris_config.json in RAM matches the SD card copy."""
    try:
        r = subprocess.run(
            ["bash", "-c",
             f"md5sum {CONFIG_FILE} {SD_CONFIG} 2>/dev/null | awk '{{print $1}}' | sort -u | wc -l"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "1"
    except Exception:
        return False

# ── TTS playback (async, non-blocking) ────────────────────────────────────────
_speak_lock = threading.Lock()

def _speak_worker(text: str, cfg: dict):
    """Synthesize via Kokoro direct (reads voice/speed from cfg); Piper fallback."""
    with _speak_lock:
        pcm = None
        try:
            import miniaudio
            voice   = cfg.get("KOKORO_VOICE", "bm_lewis")
            speed   = float(cfg.get("KOKORO_SPEED", 1.0))
            payload = {"model": "kokoro", "input": text, "voice": voice,
                       "response_format": "wav", "speed": speed}
            resp = requests.post(f"{KOKORO_URL}/v1/audio/speech", json=payload, timeout=30)
            resp.raise_for_status()
            decoded = miniaudio.decode(resp.content,
                                       output_format=miniaudio.SampleFormat.SIGNED16,
                                       nchannels=1, sample_rate=48000)
            pcm = bytes(decoded.samples)
            print(f"[WEB-TTS] Kokoro OK {len(pcm)}b voice={voice}", flush=True)
        except Exception as e:
            print(f"[WEB-TTS] Kokoro failed ({e}), falling back to Piper", flush=True)
            try:
                from services.tts import synthesize
                pcm = synthesize(text)
            except Exception as e2:
                print(f"[WEB-TTS] Piper fallback failed: {e2}", flush=True)
        if not pcm:
            return
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48000)
                wf.writeframes(pcm)
            subprocess.run(["aplay", "-q", wav_path])
            os.unlink(wav_path)
            print(f"[WEB-TTS] played {len(pcm)}b PCM", flush=True)
        except Exception as e:
            print(f"[WEB-TTS] playback error: {e}", flush=True)

def speak_async(text: str, cfg: dict):
    threading.Thread(target=_speak_worker, args=(text, cfg), daemon=True).start()

# ── routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with open(HTML_FILE) as f: return f.read()

@app.route("/iris_web.css")
def iris_css():
    with open(CSS_FILE) as f: return f.read(), 200, {"Content-Type": "text/css; charset=utf-8"}

@app.route("/iris_web.js")
def iris_js():
    with open(JS_FILE) as f: return f.read(), 200, {"Content-Type": "application/javascript; charset=utf-8"}

@app.route("/help")
def help_page():
    with open(HELP_FILE) as f: return f.read()

@app.route("/api/status")
def api_status():
    running = subprocess.run(["systemctl","is-active","assistant"],
                             capture_output=True, text=True).stdout.strip() == "active"
    return jsonify(cpu_temp=cpu_temp(), running=running, uptime=uptime_str(),
                   sleeping=os.path.exists(SLEEP_FLAG))

@app.route("/api/sysstat")
def api_sysstat():
    """Live resource snapshot for the WebUI monitor (RD-032). Computed on request
    only — NEVER written to any log/file (RD-031: no new disk writers). Disk-first:
    overlay % used, SD % used, journal size, /home/pi/logs size, plus
    load/mem/temp/uptime/throttle and a 60-sample trend from res_trend.csv."""
    import re as _re
    def _sh(c):
        try:
            return subprocess.check_output(["bash", "-c", c], text=True,
                                           stderr=subprocess.DEVNULL, timeout=5).strip()
        except Exception:
            return ""
    try:
        load1, load5, load15 = open("/proc/loadavg").read().split()[:3]
    except Exception:
        load1 = load5 = load15 = "?"
    mem_total = mem_used = mem_avail = 0
    try:
        parts = _sh("free -m | awk '/^Mem:/{print $2,$3,$7}'").split()
        if len(parts) == 3:
            mem_total, mem_used, mem_avail = (int(x) for x in parts)
    except Exception:
        pass
    overlay_pct = _sh("df -h / | awk 'NR==2{print $5}'")
    sd_pct      = _sh("df -h /media/root-ro | awk 'NR==2{print $5}'")
    journal     = _sh("journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMGB]+' | tail -1")
    logs_mb     = _sh("du -sm /home/pi/logs 2>/dev/null | cut -f1")
    throttled   = _sh("vcgencmd get_throttled 2>/dev/null | cut -d= -f2")
    trend = []
    for ln in _sh("tail -n 60 /home/pi/logs/res_trend.csv 2>/dev/null").splitlines():
        d = {}
        for tok in ln.split(","):
            if "=" in tok:
                k, v = tok.split("=", 1)
                d[k] = v
        if not d:
            continue
        trend.append({
            "ts":        ln.split(",", 1)[0],
            "load":      d.get("load"),
            "overlay":   d.get("overlay"),
            "journalMB": _re.sub(r"[^0-9.]", "", d.get("journal", "")) or None,
            "logsMB":    d.get("logsMB"),
            "temp":      _re.sub(r"[^0-9.]", "", d.get("temp", "")) or None,
        })
    return jsonify(
        load=[load1, load5, load15], ncpu=(os.cpu_count() or 4),
        mem_used_mb=mem_used, mem_avail_mb=mem_avail, mem_total_mb=mem_total,
        temp_c=cpu_temp(), uptime=uptime_str(),
        overlay_pct=overlay_pct, sd_pct=sd_pct,
        journal=journal, logs_mb=logs_mb, throttled=throttled,
        trend=trend,
    )

@app.route("/api/config", methods=["GET","POST"])
def api_config():
    if request.method == "POST":
        patch = request.get_json(force=True)
        write_cfg(patch)
        if "OWW_THRESHOLD" in patch:
            threading.Timer(0.3, lambda: subprocess.Popen(["sudo","systemctl","restart","assistant"])).start()
            return jsonify(ok=True, restarting_assistant=True)
        return jsonify(ok=True)
    # Return all overridable defaults merged with current iris_config.json overrides
    # so web UI form fields always show the current effective value.
    try:
        import core.config as _cc
        merged = {k: getattr(_cc, k) for k in _cc._OVERRIDABLE if hasattr(_cc, k)}
    except Exception:
        merged = {}
    merged.update(read_cfg())
    return jsonify(merged)

@app.route("/api/teensy", methods=["POST"])
def api_teensy():
    cmd = request.get_json(force=True).get("cmd","")
    return jsonify(ok=send_teensy(cmd), sent=cmd)

@app.route("/api/sleep_state")
def api_sleep_state():
    return jsonify(sleeping=os.path.exists(SLEEP_FLAG))

@app.route("/api/sleep", methods=["POST"])
def api_sleep():
    # EYES:SLEEP alone — CMD listener calls _do_sleep() which sends MOUTH:8 +
    # MOUTH_INTENSITY directly to Teensy. Extra MOUTH: UDP sends trigger auto-wake.
    ok = send_teensy("EYES:SLEEP")
    open(SLEEP_FLAG, "w").close()
    return jsonify(ok=ok, sleeping=True)

@app.route("/api/wake", methods=["POST"])
def api_wake():
    # EYES:WAKE alone — CMD listener calls _do_wake() which sends MOUTH:0 +
    # MOUTH_INTENSITY. Extra sends are redundant but harmless; removed for symmetry.
    ok = send_teensy("EYES:WAKE")
    if os.path.exists(SLEEP_FLAG): os.remove(SLEEP_FLAG)
    return jsonify(ok=ok, sleeping=False)

_SLEEP_CFG_KEYS = {
    "speed":          "SLEEP_ANIM_SPEED",
    "starBrightMin":  "SLEEP_ANIM_STAR_BRIGHT_MIN",
    "starBrightMax":  "SLEEP_ANIM_STAR_BRIGHT_MAX",
    "starTwinkleAmp": "SLEEP_ANIM_STAR_TWINKLE",
    "shootCount":     "SLEEP_ANIM_SHOOT_COUNT",
    "shootSpeed":     "SLEEP_ANIM_SHOOT_SPEED",
    "shootLen":       "SLEEP_ANIM_SHOOT_LEN",
    "shootBright":    "SLEEP_ANIM_SHOOT_BRIGHT",
    "warpCount":      "SLEEP_ANIM_WARP_COUNT",
    "warpSpeed":      "SLEEP_ANIM_WARP_SPEED",
    "warpBright":     "SLEEP_ANIM_WARP_BRIGHT",
    "moonR":          "SLEEP_ANIM_MOON_R",
    "moonDrift":      "SLEEP_ANIM_MOON_DRIFT",
    "saturnR":        "SLEEP_ANIM_SATURN_R",
    "saturnDrift":    "SLEEP_ANIM_SATURN_DRIFT",
    "nebulaAlpha":    "SLEEP_ANIM_NEBULA_ALPHA",
    "waveAmp0":       "SLEEP_ANIM_WAVE_AMP0",
    "waveAmp1":       "SLEEP_ANIM_WAVE_AMP1",
    "waveAmp2":       "SLEEP_ANIM_WAVE_AMP2",
    "waveOscAmp":     "SLEEP_ANIM_WAVE_OSC_AMP",
    "mouthPulseAlpha":"SLEEP_ANIM_MOUTH_PULSE_A",
    "zzzAlpha0":      "SLEEP_ANIM_ZZZ_ALPHA0",
    "zzzAlpha1":      "SLEEP_ANIM_ZZZ_ALPHA1",
    "zzzAlpha2":      "SLEEP_ANIM_ZZZ_ALPHA2",
}

@app.route("/api/sleep_cfg", methods=["GET","POST"])
def api_sleep_cfg():
    if request.method == "POST":
        patch = request.get_json(force=True) or {}
        # Map short key names to SLEEP_ANIM_* config keys and write
        cfg_patch = {}
        for short_key, val in patch.items():
            cfg_key = _SLEEP_CFG_KEYS.get(short_key)
            if cfg_key:
                cfg_patch[cfg_key] = val
        if cfg_patch:
            write_cfg(cfg_patch)
        return jsonify(ok=True)
    # GET: return current values keyed by short names
    try:
        import core.config as _cc
        live = read_cfg()
        result = {}
        for short_key, cfg_key in _SLEEP_CFG_KEYS.items():
            result[short_key] = live.get(cfg_key, getattr(_cc, cfg_key, None))
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/logs")
def api_logs():
    # 1. Current journalctl (live, current boot — last 1000 lines)
    try:
        raw = subprocess.check_output(
            ["journalctl", "-u", "assistant", "-n", "1000", "--no-pager", "--output=short-iso"],
            text=True, stderr=subprocess.DEVNULL).strip().splitlines()
    except Exception as e:
        raw = [f"ERR journalctl: {e}"]

    events = []
    for line in raw:
        ts_m  = _TS_RE.match(line)
        ts    = ts_m.group(1) if ts_m else ""
        ts_s  = ts[11:19] if len(ts) >= 19 else ""
        msg_m = _MSG_RE.search(line)
        msg   = msg_m.group(1).strip() if msg_m else ""
        if not msg:
            continue
        ev = _parse_event_msg(ts, ts_s, msg)
        if ev:
            events.append(ev)

    # 2. iris_intent.log
    try:
        with open("/home/pi/logs/iris_intent.log", encoding="utf-8") as f:
            intent_lines = [l.rstrip() for l in f.readlines()[-80:] if l.strip()]
        for il in intent_lines:
            lower = il.lower()
            ts_m2 = _TS_RE.search(il)
            ts2   = ts_m2.group(1) if ts_m2 else ""
            ts2_s = ts2[11:19] if len(ts2) >= 19 else ""
            if any(p in lower for p in _DRIFT_SIGNALS):
                cat = "drift"
            elif "stop" in lower or "reflex" in lower:
                cat = "stop"
            elif "err" in lower:
                cat = "error"
            else:
                cat = "route"
            events.append({"t": ts2, "ts": ts2_s, "cat": cat,
                            "msg": il[:200], "detail": ""})
    except Exception:
        pass

    # 3. SD daily log files — persistent history across reboots (30 days)
    events.extend(_sd_events())

    # 4. Deduplicate by (timestamp[:19], msg[:50]), sort, cap at 200
    seen, merged = set(), []
    for ev in events:
        key = (ev.get("t", "")[:19], ev.get("msg", "")[:50])
        if key not in seen:
            seen.add(key)
            merged.append(ev)
    merged.sort(key=lambda e: e.get("t", ""))
    return jsonify(events=merged[-200:])


@app.route("/api/gesture_log")
def api_gesture_log():
    """Return recent gesture events from SD history + current journal."""
    all_evs = []
    for ev in _sd_events():
        if ev.get("cat") == "gesture":
            all_evs.append(ev)
    try:
        raw = subprocess.check_output(
            ["journalctl", "-u", "assistant", "-n", "500", "--no-pager", "--output=short-iso"],
            text=True, stderr=subprocess.DEVNULL).strip().splitlines()
    except Exception:
        raw = []
    for line in raw:
        ts_m  = _TS_RE.match(line)
        ts    = ts_m.group(1) if ts_m else ""
        ts_s  = ts[11:19] if len(ts) >= 19 else ""
        msg_m = _MSG_RE.search(line)
        msg   = msg_m.group(1).strip() if msg_m else ""
        if not msg:
            continue
        ev = _parse_event_msg(ts, ts_s, msg)
        if ev and ev.get("cat") == "gesture":
            all_evs.append(ev)
    seen, result = set(), []
    for ev in all_evs:
        key = (ev.get("t", "")[:19], ev.get("msg", "")[:50])
        if key not in seen:
            seen.add(key)
            result.append(ev)
    result.sort(key=lambda e: e.get("t", ""))
    return jsonify(events=result[-200:])


# ── Gesture activity monitor (live per-direction hit counts) ──────────────────
# Computed on request from the journal, NEVER logged (RD-031/RD-032 pattern).
# The T4.0 bridge logs every gesture the PAJ7620U2 emits as
# "[GESTURE] gesture=<RAW> action=<MAPPED>". Aggregating by RAW direction lets
# the WebUI show, in real time, which physical swipes the sensor is actually
# detecting (e.g. swipe-up = VOL+ staying at 0 means the sensor never reports
# the Up bit — a sensitivity/FOV problem, not a mapping bug).
_GESTURE_TOKENS = ("VOL+", "VOL-", "STOP", "RIGHT", "FORWARD", "BACKWARD", "CW", "CCW")
_GEST_DIR_LABEL = {
    "VOL+": "Swipe Up", "VOL-": "Swipe Down", "STOP": "Swipe Left",
    "RIGHT": "Swipe Right", "FORWARD": "Push (toward)", "BACKWARD": "Pull (away)",
    "CW": "Rotate CW", "CCW": "Rotate CCW",
}
_GEST_LINE_RE = re.compile(r'\[GESTURE\]\s+gesture=(\S+)\s+action=(\S+)')

@app.route("/api/gesture_stats")
def api_gesture_stats():
    """Per-direction gesture hit counts + last-seen, from the current journal.
    Cheap: pre-filtered to [GESTURE] lines by grep before Python parses."""
    counts = {g: 0 for g in _GESTURE_TOKENS}
    last   = {g: "" for g in _GESTURE_TOKENS}
    try:
        raw = subprocess.check_output(
            ["bash", "-c",
             "journalctl -u assistant -n 6000 --no-pager --output=short-iso "
             "| grep -F '[GESTURE] gesture=' | tail -500"],
            text=True, stderr=subprocess.DEVNULL).splitlines()
    except Exception:
        raw = []
    total = 0
    for line in raw:
        ts_m = _TS_RE.match(line)
        ts_s = ts_m.group(1)[11:19] if ts_m else ""
        m = _GEST_LINE_RE.search(line)
        if not m:
            continue
        g = m.group(1)
        if g in counts:
            counts[g] += 1
            last[g]    = ts_s
            total     += 1
    labels = {g: _GEST_DIR_LABEL[g] for g in _GESTURE_TOKENS}
    return jsonify(counts=counts, last=last, labels=labels,
                   order=list(_GESTURE_TOKENS), total=total)


# ── Person Sensor live status (T4.1 eye-tracking sensor health/activity) ──────
# Computed on request from the journal, NEVER logged (RD-031/RD-032 pattern).
# The teensy bridge echoes the T4.1's "[DBG] Person Sensor ..." probe results and
# FACE:1/FACE:0 tracking state as "[EYES] << ..." journal lines. This surfaces the
# exact hardware condition behind RD-033 (no I2C ACK at 0x62 = sensor dead/loose)
# plus live face-acquire/lost activity, so the operator can see sensor health on
# the WebUI instead of reading the journal.
_PS_FACE1_RE = re.compile(r'\bFACE:1\b')
_PS_FACE0_RE = re.compile(r'\bFACE:0\b')

@app.route("/api/ps/status")
def api_ps_status():
    try:
        raw = subprocess.check_output(
            ["bash", "-c",
             "journalctl -u assistant -n 6000 --no-pager --output=short-iso "
             "| grep -E 'Person Sensor detected|no ACK at 0x62|No Person Sensor|FACE:[01]' "
             "| tail -250"],
            text=True, stderr=subprocess.DEVNULL).splitlines()
    except Exception:
        raw = []
    last = {"detected": "", "absent": "", "face1": "", "face0": ""}
    acquisitions = 0
    recent = []
    for line in raw:
        ts_m = _TS_RE.match(line)
        ts   = ts_m.group(1) if ts_m else ""
        ts_s = ts[11:19] if len(ts) >= 19 else ""
        if "Person Sensor detected" in line:
            last["detected"] = ts
            recent.append({"ts": ts_s, "kind": "detected", "msg": "Person Sensor detected"})
        elif "no ACK at 0x62" in line or "No Person Sensor" in line:
            last["absent"] = ts
        elif _PS_FACE1_RE.search(line):
            last["face1"] = ts
            acquisitions += 1
            recent.append({"ts": ts_s, "kind": "track", "msg": "Face acquired (FACE:1)"})
        elif _PS_FACE0_RE.search(line):
            last["face0"] = ts
            recent.append({"ts": ts_s, "kind": "lost", "msg": "Face lost (FACE:0)"})
    detected_ts, absent_ts = last["detected"], last["absent"]
    f1, f0 = last["face1"], last["face0"]
    # Any detection OR FACE report is positive proof the sensor is alive on the bus.
    # The firmware's "no ACK" probe lines are bounded to the first ~30 cold-boot
    # attempts (pre-NTP timestamps) and go silent afterward, so a stale "no ACK"
    # must NOT be read as "dead" once real face activity has appeared. Sensor is
    # only "searching" when no-ACK is the single most-recent signal.
    alive_ts = max(detected_ts, f1, f0)   # ISO strings compare correctly; "" < any ts
    present = bool(alive_ts) and alive_ts >= absent_ts
    if present:
        if f1 and f1 >= f0:
            state = "tracking"; label = "TRACKING — face locked"
        else:
            state = "idle";     label = "PRESENT — sensor live, no face in view"
    elif absent_ts:
        state = "searching"
        label = ("SEARCHING — no I2C ACK at 0x62 yet "
                 "(cold-boot probe window, or loose/intermittent connector)")
    else:
        state = "unknown"; label = "UNKNOWN — no recent sensor signal in journal"
    return jsonify(state=state, label=label, present=present,
                   last_detected=last["detected"], last_absent=last["absent"],
                   last_face1=last["face1"], last_face0=last["face0"],
                   acquisitions=acquisitions, recent=list(reversed(recent[-25:])))


_KOKORO_FALLBACK_VOICES = [
    "af_alloy", "af_bella", "af_heart", "af_jessica", "af_nicole", "af_nova",
    "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_liam",
    "am_michael", "am_onyx", "bf_alice", "bf_emma", "bf_isabella",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis", "bm_myles",
]

@app.route("/api/kokoro_voices")
def api_kokoro_voices():
    try:
        r = requests.get(f"{KOKORO_URL}/v1/voices", timeout=5)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            voices = data
        elif isinstance(data, dict):
            voices = data.get("voices", data.get("data", _KOKORO_FALLBACK_VOICES))
        else:
            voices = _KOKORO_FALLBACK_VOICES
        return jsonify(voices=voices)
    except Exception as e:
        return jsonify(voices=_KOKORO_FALLBACK_VOICES, error=str(e))

@app.route("/api/restart", methods=["POST"])
def api_restart():
    subprocess.Popen(["sudo","systemctl","restart","assistant"]); return jsonify(ok=True)

@app.route("/api/vram")
def api_vram():
    try:
        r = requests.get(f"http://{GANDALF}:{OLLAMA_PORT}/api/ps", timeout=5)
        return jsonify(r.json())
    except Exception as e: return jsonify(error=str(e)), 503

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data  = request.get_json(force=True)
    text  = data.get("text","").strip()
    speak = bool(data.get("speak", False))
    mode  = data.get("mode", "adult")
    if not text: return jsonify(error="empty"), 400
    cfg   = read_cfg()
    model = cfg.get("OLLAMA_MODEL_KIDS" if mode == "kids" else "OLLAMA_MODEL_ADULT", "iris")
    try:
        import datetime as _dt
        _now = _dt.datetime.now()
        _sys = f"Current date and time: {_now.strftime('%A, %B %d %Y, %I:%M %p')} Mountain Time."
        r = requests.post(f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
            json={"model": model, "prompt": text, "system": _sys, "stream": False},
            timeout=90)
        r.raise_for_status()
        raw_reply = r.json().get("response", "").strip()
        from services.llm import extract_emotion_from_reply, clean_llm_reply
        emotion, clean_reply = extract_emotion_from_reply(raw_reply)
        clean_reply = clean_llm_reply(clean_reply)
        if speak and clean_reply:
            speak_async(clean_reply, cfg)
        return jsonify(reply=clean_reply, emotion=emotion, spoken=speak and bool(clean_reply))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/speak", methods=["POST"])
def api_speak():
    """Speak text verbatim via TTS — no LLM."""
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify(error="empty"), 400
    cfg = read_cfg()
    speak_async(text, cfg)
    return jsonify(ok=True, spoken=text)

@app.route("/api/sd_status")
def api_sd_status():
    """Check if iris_config.json in RAM matches the SD card copy."""
    return jsonify(synced=_sd_synced())

@app.route("/api/persist_config", methods=["POST"])
def api_persist_config():
    """Copy iris_config.json through overlayfs to SD card. Returns ok + verified."""
    # RD-034 S153: snapshot config under _cfg_lock to a tmp file before the
    # subprocess cp -- eliminates same-inode self-copy: when overlay has no
    # upper-layer entry, RAM and SD are the same inode; cp O_TRUNC zeroes
    # CONFIG_FILE before reading it. Lock released before subprocess.
    _persist_src = CONFIG_FILE + ".persist_tmp"
    with _cfg_lock:
        try:
            if not os.path.exists(CONFIG_FILE) or os.path.getsize(CONFIG_FILE) < 3:
                return jsonify(ok=False, error="iris_config.json empty/missing -- refusing to persist (would clobber SD copy)"), 400
            _blob = open(CONFIG_FILE).read()
            json.loads(_blob)
        except Exception as e:
            return jsonify(ok=False, error=f"iris_config.json invalid -- refusing to persist: {e}"), 400
        try:
            with open(_persist_src, "w") as _f: _f.write(_blob)
        except Exception as e:
            return jsonify(ok=False, error=f"persist snapshot failed: {e}"), 500
    try:
        result = subprocess.run(
            ["sudo", "bash", "-c",
             f"mount -o remount,rw /media/root-ro && "
             f"[ -s {SD_CONFIG} ] && cp -f {SD_CONFIG} {SD_CONFIG}.goldbak; "
             f"cp {_persist_src} {SD_CONFIG} && "
             f"chown pi:pi {SD_CONFIG} && "
             f"chmod 644 {SD_CONFIG} && "
             f"sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return jsonify(ok=False, error=result.stderr.strip() or "mount/cp failed"), 500
        verified = _sd_synced()
        # Copy ALSA state to SD layer
        alsa_src = "/var/lib/alsa/asound.state"
        alsa_dst = "/media/root-ro/var/lib/alsa/asound.state"
        alsa_result = subprocess.run(
            ["sudo", "bash", "-c",
             f"mount -o remount,rw /media/root-ro && "
             f"cp {alsa_src} {alsa_dst} && "
             f"sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        alsa_ok = alsa_result.returncode == 0
        return jsonify(ok=verified, verified=verified, alsa_persisted=alsa_ok)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/volume", methods=["GET","POST"])
def api_volume():
    """Get or set wm8960 speaker volume (0-127). POST accepts {"level": <abs>} or {"delta": <±n>}."""
    import re as _re
    card = subprocess.check_output(
        ["bash","-c","aplay -l 2>/dev/null | grep wm8960 | head -1 | awk '{print $2}' | tr -d ':'"],
        text=True).strip() or "0"
    if request.method == "POST":
        data = request.get_json(force=True)
        if "delta" in data:
            out = subprocess.check_output(["amixer","-c",card,"sget","Speaker"], text=True)
            current = 110
            for line in out.splitlines():
                if "Front Left:" in line:
                    m = _re.search(r"Playback (\d+)", line)
                    if m:
                        current = int(m.group(1))
                        break
            level = max(0, min(127, current + int(data["delta"])))
        else:
            level = max(0, min(127, int(data.get("level", 110))))
        subprocess.run(["amixer","-c",card,"sset","Speaker",str(level)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["alsactl", "store"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        write_cfg({"SPEAKER_VOLUME": level})
        return jsonify(ok=True, level=level, pct=round(level/127*100))
    out = subprocess.check_output(["amixer","-c",card,"sget","Speaker"], text=True)
    for line in out.splitlines():
        if "Front Left:" in line:
            m = _re.search(r"Playback (\d+)", line)
            if m:
                level = int(m.group(1))
                return jsonify(level=level, pct=round(level/127*100))
    return jsonify(level=110, pct=87)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Interrupt current TTS playback."""
    ok = send_teensy("STOP_PLAYBACK")
    return jsonify(ok=ok)


@app.route("/api/listen", methods=["POST"])
def api_listen():
    """Trigger a manual listen cycle without saying the wakeword."""
    try:
        open("/tmp/iris_manual_listen", "w").close()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/bench")
def api_bench():
    """Parse recent [BENCH] log lines and return structured cycle data + tuning levers."""
    import re as _re
    try:
        raw = subprocess.check_output(
            ["journalctl", "-u", "assistant", "-n", "600", "--no-pager", "--output=short-iso"],
            text=True, stderr=subprocess.DEVNULL)
        lines = raw.splitlines()
    except Exception as e:
        return jsonify(error=str(e), cycles=[], levers={})

    def _parse(line):
        m = _re.search(r'\[BENCH\](.*)', line)
        if not m: return None
        kv = {}
        for part in m.group(1).split():
            if '=' in part:
                k, v = part.split('=', 1)
                kv[k] = v.strip('"\'')
        return kv if kv else None

    cycles, cur = [], {}
    for line in lines:
        kv = _parse(line)
        if not kv: continue
        stage = kv.get('stage')
        if not stage: continue
        if stage == 'wake_detected':
            if cur: cycles.append(cur)
            cur = {'trigger': kv.get('trigger', '?'), 't': kv.get('t', '')}
        elif cur:
            cur[stage] = kv
    if cur:
        cycles.append(cur)

    # Fallback: if journal has no cycles (e.g. after reboot), read from persistent JSONL
    if not cycles:
        try:
            from core.config import BENCH_LOG as _BENCH_LOG
            with open(_BENCH_LOG, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        rec = json.loads(_line)
                        st  = rec.get("stages", {})
                        try:
                            from datetime import datetime as _dt
                            _t = str(_dt.fromisoformat(rec["ts"]).timestamp())
                        except Exception:
                            _t = ""
                        cycle = {
                            "trigger":       "wake",
                            "t":             _t,
                            "_from_jsonl":   True,
                            "rec_done":      {"dur_rec":   f"{st.get('record_duration_ms',0)/1000:.2f}"},
                            "stt_done":      {"dur_stt":   f"{st.get('stt_ms',0)/1000:.2f}",
                                              "transcript": rec.get("transcript", "")},
                            "llm_start":     {"tier":        st.get("tier", "-"),
                                              "num_predict": st.get("num_predict", "-")},
                            "llm_first_chunk": {"dur_ttfc": f"{st.get('llm_first_token_ms',0)/1000:.2f}"},
                            "llm_done":      {"dur_llm":   f"{st.get('llm_total_ms',0)/1000:.2f}"},
                            "tts_done":      {"dur_tts":   f"{st.get('tts_ms',0)/1000:.2f}",
                                              "engine":      st.get("engine", "-")},
                            "audio_done":    {"dur_audio": "-",
                                              "dur_total":   f"{st.get('play_start_ms',0)/1000:.2f}"},
                        }
                        if rec.get("emotion"):
                            cycle["emotion"] = rec["emotion"]
                        cycles.append(cycle)
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        import core.config as _cc
        levers = {k: getattr(_cc, k) for k in
                  ('NUM_PREDICT_SHORT', 'NUM_PREDICT_MEDIUM', 'NUM_PREDICT_LONG',
                   'NUM_PREDICT_MAX', 'TTS_MAX_CHARS', 'KOKORO_ENABLED')
                  if hasattr(_cc, k)}
    except Exception:
        levers = {}

    return jsonify(cycles=cycles[-20:], levers=levers)


@app.route("/api/vision", methods=["POST"])
def api_vision():
    """Capture Pi camera frame, send to Ollama vision model, return description."""
    data   = request.get_json(force=True)
    prompt = data.get("prompt", "Describe in detail what you see.").strip()
    speak  = bool(data.get("speak", False))
    try:
        import base64, tempfile as _tf
        cfg = read_cfg()
        with _tf.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            img_path = f.name
        result = subprocess.run(
            ["rpicam-still", "-o", img_path, "--nopreview", "-t", "500",
             "--width", "1024", "--height", "768"],
            capture_output=True, timeout=15)
        if result.returncode != 0:
            try: os.unlink(img_path)
            except Exception: pass
            return jsonify(error="Camera capture failed: " + result.stderr.decode()[:200]), 500
        with open(img_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
        try: os.unlink(img_path)
        except Exception: pass
        model = cfg.get("VISION_MODEL", "iris")
        # num_ctx 6144: a camera frame encodes to ~4570 vision tokens on
        # mistral-small3.2:24b, overflowing the old 4096 default context
        # (HTTP 400). Matches the modelfile num_ctx since S119b. (S118)
        r = requests.post(
            f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
            json={"model": model, "prompt": prompt, "images": [image_b64],
                  "stream": False,
                  "options": {"num_ctx": cfg.get("VISION_NUM_CTX", 6144)}},
            timeout=120)
        r.raise_for_status()
        raw_reply = r.json().get("response", "").strip()
        from services.llm import extract_emotion_from_reply, clean_llm_reply
        emotion, clean_reply = extract_emotion_from_reply(raw_reply)
        clean_reply = clean_llm_reply(clean_reply)
        if speak and clean_reply:
            speak_async(clean_reply, cfg)
        return jsonify(reply=clean_reply, emotion=emotion, spoken=speak and bool(clean_reply))
    except Exception as e:
        return jsonify(error=str(e)), 500


_post_lock    = threading.Lock()
_post_running = threading.Event()
_post_last_result = None   # type: dict | None


@app.route("/api/post", methods=["GET", "POST"])
def api_post():
    global _post_last_result
    if request.method == "GET":
        return jsonify(running=_post_running.is_set(), result=_post_last_result)
    if _post_running.is_set():
        return jsonify(ok=False, error="POST already running"), 409

    def _do_post():
        global _post_last_result
        _post_running.set()
        try:
            sys.path.insert(0, "/home/pi")
            import importlib
            import iris_post as _ip
            importlib.reload(_ip)
            _post_last_result = _ip.run_post(verbose=True)
        except Exception as e:
            _post_last_result = {
                "verdict": "ERROR", "error": str(e),
                "n_pass": 0, "n_warn": 0, "n_fail": 0, "n_total": 0,
                "checks": [], "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }
        finally:
            _post_running.clear()

    threading.Thread(target=_do_post, daemon=True).start()
    return jsonify(ok=True, started=True)


# NOTE: These defaults intentionally differ from hardware/base_mount_bridge._DEFAULT_GESTURE_MAP
# (BACKWARD=SLEEP vs WAKE, CW=VOL+ vs MUTE, CCW=VOL- vs SKIP).
# This map is used ONLY for web-UI display (GET /api/gesture_config).
# Live runtime behavior is controlled exclusively by GESTURE_MAP in iris_config.json.
_DEFAULT_GESTURE_MAP = {
    "VOL+":    "VOL+",
    "VOL-":    "VOL-",
    "STOP":    "STOP",
    "RIGHT":   "STOP",
    "LISTEN":  "LISTEN",
    "FORWARD": "LISTEN",
    "BACKWARD":"SLEEP",
    "CW":      "VOL+",
    "CCW":     "VOL-",
}
_VALID_GESTURE_ACTIONS = {"VOL+", "VOL-", "STOP", "LISTEN", "SLEEP", "WAKE", "MUTE", "SKIP"}


@app.route("/api/gesture_config", methods=["GET", "POST"])
def api_gesture_config():
    if request.method == "POST":
        data = request.get_json(force=True)
        raw_map = data.get("GESTURE_MAP", {})
        cleaned = {k: v for k, v in raw_map.items() if v in _VALID_GESTURE_ACTIONS}
        threshold = max(0, min(255, int(data.get("GESTURE_PROXIMITY_THRESHOLD", 150))))
        write_cfg({"GESTURE_MAP": cleaned, "GESTURE_PROXIMITY_THRESHOLD": threshold})
        return jsonify(ok=True)
    cfg = read_cfg()
    stored = cfg.get("GESTURE_MAP", {})
    merged = dict(_DEFAULT_GESTURE_MAP)
    merged.update(stored)   # overlay stored values; new keys keep defaults
    return jsonify(
        GESTURE_MAP=merged,
        GESTURE_PROXIMITY_THRESHOLD=cfg.get("GESTURE_PROXIMITY_THRESHOLD", 150),
    )


@app.route("/api/model_state")
def api_model_state():
    try:
        r = requests.post(f"http://{GANDALF}:{OLLAMA_PORT}/api/show",
                          json={"name": "iris"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        modelfile = data.get("modelfile", "")
        return jsonify(
            ok=True,
            model="iris",
            modelfile_excerpt=modelfile[:300],
            modified_at=data.get("modified_at"),
            raw=data
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/rebuild_model", methods=["POST"])
def api_rebuild_model():
    data = request.get_json(force=True) or {}
    target = data.get("model", "iris")
    if target not in ("iris", "iris-kids", "both"):
        return jsonify(ok=False, error="model must be iris, iris-kids, or both"), 400

    secrets_path = "/home/pi/.iris_secrets"
    try:
        secrets = {}
        with open(secrets_path) as sf:
            for line in sf:
                line = line.strip()
                if "=" in line:
                    k, _, v = line.partition("=")
                    secrets[k.strip()] = v.strip()
        ssh_user = secrets.get("GANDALF_SSH_USER", "")
        ssh_pass = secrets.get("GANDALF_SSH_PASS", "")
        if not ssh_user or not ssh_pass:
            return jsonify(ok=False,
                           error="Configure /home/pi/.iris_secrets on Pi4 to enable model rebuild"), 500
    except FileNotFoundError:
        return jsonify(ok=False,
                       error="Configure /home/pi/.iris_secrets on Pi4 to enable model rebuild"), 500
    except Exception as e:
        return jsonify(ok=False, error=f"Secrets file error: {e}"), 500

    model_files = {
        "iris":      r"C:\IRIS\IRIS-Robot-Face\ollama\iris_modelfile.txt",
        "iris-kids": r"C:\IRIS\IRIS-Robot-Face\ollama\iris-kids_modelfile.txt",
    }
    targets = ["iris", "iris-kids"] if target == "both" else [target]
    outputs = []
    for t in targets:
        cmd = f"ollama create {t} -f {model_files[t]}"
        try:
            result = subprocess.run(
                ["sshpass", "-p", ssh_pass, "ssh",
                 "-o", "StrictHostKeyChecking=no",
                 f"{ssh_user}@192.168.1.3", cmd],
                capture_output=True, text=True, timeout=120
            )
            outputs.append(f"=== {t} ===\n{result.stdout}{result.stderr}".strip())
        except FileNotFoundError:
            return jsonify(ok=False,
                           error="sshpass not found on Pi4; install: apt-get install sshpass"), 500
        except subprocess.TimeoutExpired:
            return jsonify(ok=False, error=f"Rebuild of {t} timed out after 120s"), 500
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True, output="\n\n".join(outputs))


_VALID_EMOTIONS_SET = {"NEUTRAL","HAPPY","CURIOUS","ANGRY","SLEEPY","SURPRISED","SAD","CONFUSED","AMUSED"}
_DEFAULT_MOUTH_MAP  = {"NEUTRAL":0,"HAPPY":1,"CURIOUS":2,"ANGRY":3,"SLEEPY":4,
                        "SURPRISED":5,"SAD":6,"CONFUSED":7,"AMUSED":2}
_MOUTH_COUNT = 10   # indices 0-9 (0-8 original + 9=SILLY)
_EYE_COUNT   = 8    # indices 0-7

@app.route("/api/emotion_map", methods=["GET","POST"])
def api_emotion_map():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        raw_mouth = data.get("EMOTION_MOUTH_MAP", {})
        raw_eye   = data.get("EMOTION_EYE_MAP", {})
        clean_mouth = {}
        for k, v in raw_mouth.items():
            try:
                iv = int(v)
                if k in _VALID_EMOTIONS_SET and 0 <= iv < _MOUTH_COUNT:
                    clean_mouth[k] = iv
            except (ValueError, TypeError):
                pass
        clean_eye = {}
        for k, v in raw_eye.items():
            try:
                iv = int(v)
                if k in _VALID_EMOTIONS_SET and -1 <= iv < _EYE_COUNT:
                    clean_eye[k] = iv
            except (ValueError, TypeError):
                pass
        print(f"[EMAP] POST mouth={clean_mouth} eye={clean_eye}")
        write_cfg({"EMOTION_MOUTH_MAP": clean_mouth, "EMOTION_EYE_MAP": clean_eye})
        return jsonify(ok=True)
    cfg      = read_cfg()
    m_map    = {**_DEFAULT_MOUTH_MAP, **cfg.get("EMOTION_MOUTH_MAP", {})}
    e_map    = {e: -1 for e in _VALID_EMOTIONS_SET}
    e_map.update(cfg.get("EMOTION_EYE_MAP", {}))
    return jsonify(mouth_map=m_map, eye_map=e_map)


# ── Person Sensor runtime config (S141, RD-033 resolved) ─────────────────────────
# The T4.1 Person Sensor params live in Teensy firmware. We expose them as runtime
# PS_CFG: serial commands: POST writes a ps_config.json sidecar AND forwards each
# value live to the Teensy (via send_teensy -> assistant.py UDP -> serial). The
# sidecar is re-pushed by assistant.py on its serial open (Teensy reboot reverts to
# firmware defaults). Persist route does the overlayfs dual-write so it survives a
# Pi4 reboot.
PS_CONFIG_FILE   = "/home/pi/ps_config.json"
SD_PS_CONFIG     = "/media/root-ro/home/pi/ps_config.json"
_PS_CFG_DEFAULTS = {"CONF": 60, "FACING": 1, "LOST_MS": 5000, "Y_BIAS": 0.0, "LED": 0}

def _read_ps_cfg():
    cfg = dict(_PS_CFG_DEFAULTS)
    try:
        with open(PS_CONFIG_FILE) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in _PS_CFG_DEFAULTS})
    except Exception:
        pass
    return cfg

@app.route("/api/ps/config", methods=["GET", "POST"])
def api_ps_config():
    """GET: merged ps_config.json over defaults. POST: validate, write sidecar,
    forward each PS_CFG:KEY=value live to the Teensy."""
    if request.method == "GET":
        return jsonify(_read_ps_cfg())
    body = request.get_json(force=True) or {}
    update = {k: body[k] for k in _PS_CFG_DEFAULTS if k in body}
    if not update:
        return jsonify(ok=False, error="no valid keys"), 400
    cfg = _read_ps_cfg()
    cfg.update(update)
    # coerce types so the sidecar and serial values are well-formed
    try:
        cfg["CONF"]    = max(0, min(100, int(float(cfg["CONF"]))))
        cfg["FACING"]  = 1 if int(float(cfg["FACING"])) else 0
        cfg["LOST_MS"] = max(0, int(float(cfg["LOST_MS"])))
        cfg["Y_BIAS"]  = round(float(cfg["Y_BIAS"]), 3)
        cfg["LED"]     = 1 if int(float(cfg["LED"])) else 0
    except (ValueError, TypeError) as e:
        return jsonify(ok=False, error=f"bad value: {e}"), 400
    try:
        _tmp = PS_CONFIG_FILE + ".tmp"
        with open(_tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(_tmp, PS_CONFIG_FILE)
    except Exception as e:
        return jsonify(ok=False, error=f"write failed: {e}"), 500
    for k in _PS_CFG_DEFAULTS:
        send_teensy(f"PS_CFG:{k}={cfg[k]}")
    print(f"[PSCFG] POST {cfg}")
    return jsonify(ok=True, **cfg)

@app.route("/api/ps/config/persist", methods=["POST"])
def api_ps_config_persist():
    """Persist ps_config.json to SD via overlayfs dual-write. Returns ok + md5."""
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"mount -o remount,rw /media/root-ro && "
             f"cp {PS_CONFIG_FILE} {SD_PS_CONFIG} && "
             f"chmod 644 {SD_PS_CONFIG} && sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return jsonify(ok=False, error=r.stderr.strip() or "persist failed"), 500
        md5_r = subprocess.run(
            ["bash", "-c", f"md5sum {PS_CONFIG_FILE} | awk '{{print $1}}'"],
            capture_output=True, text=True, timeout=5)
        match_r = subprocess.run(
            ["bash", "-c",
             f"md5sum {PS_CONFIG_FILE} {SD_PS_CONFIG} "
             f"| awk '{{print $1}}' | sort -u | wc -l"],
            capture_output=True, text=True, timeout=5)
        verified = match_r.stdout.strip() == "1"
        return jsonify(ok=verified, verified=verified, md5=md5_r.stdout.strip())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── T4.0 servo Person Sensor LED (separate Teensy, owned by base_mount_bridge) ───
# The web process can't touch /dev/ttyIRIS_SERVO, so it sends one-line commands to
# the bridge over UDP (port 10510). The bridge writes `PSLED=0/1` to the Teensy and
# re-asserts the saved state on (re)connect. Lighting the LED is an I2C write to the
# sensor, so a lit LED proves the T4.0 Person Sensor is powered and reachable.
SERVO_CMD_PORT    = 10510
SERVO_CONFIG_FILE = "/home/pi/servo_config.json"
SD_SERVO_CONFIG   = "/media/root-ro/home/pi/servo_config.json"

def _send_servo(cmd):
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.sendto((cmd.strip() + "\n").encode(), ("127.0.0.1", SERVO_CMD_PORT))
        return True
    except Exception as e:
        print(f"[WEB] servo UDP: {e}"); return False

@app.route("/api/servo/config")
def api_servo_config():
    led = 0
    try:
        with open(SERVO_CONFIG_FILE) as f:
            led = 1 if json.load(f).get("LED") else 0
    except Exception:
        pass
    return jsonify(LED=led)

@app.route("/api/servo/led", methods=["POST"])
def api_servo_led():
    body = request.get_json(force=True) or {}
    on = 1 if (body.get("LED") or body.get("on")) else 0
    try:
        tmp = SERVO_CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"LED": on}, f, indent=2)
        os.replace(tmp, SERVO_CONFIG_FILE)
    except Exception as e:
        return jsonify(ok=False, error=f"write failed: {e}"), 500
    sent = _send_servo(f"PSLED={on}")
    persisted = False
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"mount -o remount,rw /media/root-ro && cp {SERVO_CONFIG_FILE} {SD_SERVO_CONFIG} && "
             f"chmod 644 {SD_SERVO_CONFIG} && sync && mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        persisted = (r.returncode == 0)
    except Exception:
        pass
    print(f"[SERVOLED] LED={on} sent={sent} persisted={persisted}")
    return jsonify(ok=True, LED=on, sent=sent, persisted=persisted)


# ── OGLE vision node proxy (RD-033) ──────────────────────────────────────────────
# The OGLE ESP32-S3 face-tracking camera (replaces the dead Teensy Person Sensor)
# serves status/management JSON on its own static IP. We proxy it Pi4-side so the
# browser never has to cross-origin / mixed-content to the node. The gaze DATA path
# is USB-CDC (ogle_bridge) and is untouched by this — these are management-plane only.
# Mirrors /api/sysstat: computed on request, short timeout, ~1 s cache, NEVER logged.
_ogle_cache = {"t": 0.0, "data": None}

@app.route("/api/ogle")
def api_ogle():
    """Proxy OGLE GET /health (cached ~1 s). Returns {ok, ...health} or {ok:False}."""
    now = time.time()
    if _ogle_cache["data"] is not None and now - _ogle_cache["t"] < 1.0:
        return jsonify(_ogle_cache["data"])
    try:
        r = requests.get(f"{OGLE_URL}/health", timeout=1.5)
        r.raise_for_status()
        data = r.json()
        data["ok"] = True
    except Exception as e:
        data = {"ok": False, "error": str(e)[:120]}
    _ogle_cache["t"] = now
    _ogle_cache["data"] = data
    return jsonify(data)

@app.route("/api/ogle/config", methods=["GET", "POST"])
def api_ogle_config():
    """Proxy OGLE runtime tuning (confidence / facing threshold / accurate-fast mode).
    POST forwards conf/facing/mode as query args; OGLE persists them to NVS."""
    try:
        if request.method == "POST":
            body = request.get_json(force=True) or {}
            params = {}
            if "conf"   in body: params["conf"]   = body["conf"]
            if "facing" in body: params["facing"] = body["facing"]
            if "mode"   in body: params["mode"]   = body["mode"]
            r = requests.post(f"{OGLE_URL}/config", params=params, timeout=2.0)
            _ogle_cache["t"] = 0.0   # force-refresh the health cache after a change
        else:
            r = requests.get(f"{OGLE_URL}/config", timeout=1.5)
        r.raise_for_status()
        return jsonify(ok=True, **r.json())
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:120]), 502

@app.route("/api/ogle/reboot", methods=["POST"])
def api_ogle_reboot():
    """Proxy OGLE POST /reboot — remote-restart the vision node."""
    try:
        r = requests.post(f"{OGLE_URL}/reboot", timeout=2.0)
        r.raise_for_status()
        return jsonify(ok=True, **r.json())
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:120]), 502


# ── OGLE bridge tuning (env vars in ogle-bridge.service) ─────────────────────
_OGLE_SERVICE_FILE = "/etc/systemd/system/ogle-bridge.service"
_SD_OGLE_SERVICE   = "/media/root-ro/etc/systemd/system/ogle-bridge.service"
_OGLE_ENV_DEFAULTS = {
    "OGLE_FACING_REQUIRED": "1",
    "OGLE_CONF_GATE":       "60",
    "OGLE_MIN_SIZE":        "1500",
    "OGLE_LOST_TIMEOUT_S":  "1.0",
    "OGLE_FLIP_X":          "1",
    "OGLE_FLIP_Y":          "0",
    "OGLE_Y_BIAS":          "-0.10",
    "OGLE_EMA_ALPHA":       "0.4",
    "OGLE_DEADBAND":        "0.03",
    "OGLE_MAX_HZ":          "15",
}

@app.route("/api/ogle/bridge", methods=["GET", "POST"])
def api_ogle_bridge():
    """GET: return current OGLE bridge env vars from the service file.
    POST: rewrite env lines, daemon-reload, restart ogle-bridge."""
    if request.method == "GET":
        vals = dict(_OGLE_ENV_DEFAULTS)
        try:
            with open(_OGLE_SERVICE_FILE) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("Environment="):
                        kv = s[len("Environment="):]
                        k, _, v = kv.partition("=")
                        if k in _OGLE_ENV_DEFAULTS:
                            vals[k] = v
        except Exception as e:
            return jsonify(error=str(e)), 500
        return jsonify(vals)
    # POST: rewrite service file env block + restart
    update = request.get_json(force=True) or {}
    update = {k: str(v) for k, v in update.items() if k in _OGLE_ENV_DEFAULTS}
    if not update:
        # Nothing to rewrite — just reload and restart
        subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True, timeout=5)
        subprocess.run(["sudo", "systemctl", "restart", "ogle-bridge"], capture_output=True, timeout=10)
        return jsonify(ok=True)
    try:
        with open(_OGLE_SERVICE_FILE) as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    if not lines:
        return jsonify(ok=False, error="service file is empty — restore from repo before saving bridge config"), 500
    new_lines = []
    ogle_inserted = False
    in_service = False
    for line in lines:
        s = line.strip()
        if s == "[Service]":
            in_service = True
            new_lines.append(line)
            continue
        if s.startswith("[") and s != "[Service]":
            if in_service and not ogle_inserted:
                for k, v in update.items():
                    new_lines.append(f"Environment={k}={v}\n")
                ogle_inserted = True
            in_service = False
            new_lines.append(line)
            continue
        # Strip active or commented OGLE env lines for known keys
        check = s.lstrip("# ").strip()
        if check.startswith("Environment="):
            k = check[len("Environment="):].split("=", 1)[0]
            if k in _OGLE_ENV_DEFAULTS:
                if in_service and not ogle_inserted:
                    for uk, uv in update.items():
                        new_lines.append(f"Environment={uk}={uv}\n")
                    ogle_inserted = True
                continue
        new_lines.append(line)
    content = "".join(new_lines)
    if len(content) < 200:
        return jsonify(ok=False, error=f"service file rewrite produced only {len(content)} bytes — aborted to protect config"), 500
    import tempfile as _tf
    with _tf.NamedTemporaryFile(mode="w", suffix=".service", delete=False) as tf:
        tf.write(content)
        tmp = tf.name
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"cp {tmp} {_OGLE_SERVICE_FILE} && chmod 644 {_OGLE_SERVICE_FILE}"],
            capture_output=True, text=True, timeout=10)
        try: os.unlink(tmp)
        except Exception: pass
        if r.returncode != 0:
            return jsonify(ok=False, error=r.stderr.strip() or "write failed"), 500
        subprocess.run(["sudo", "systemctl", "daemon-reload"],
                       capture_output=True, timeout=5)
        subprocess.run(["sudo", "systemctl", "restart", "ogle-bridge"],
                       capture_output=True, timeout=10)
        return jsonify(ok=True)
    except Exception as e:
        try: os.unlink(tmp)
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/ogle/bridge/persist", methods=["POST"])
def api_ogle_bridge_persist():
    """Persist ogle-bridge.service to SD via overlayfs dual-write. Returns ok + md5."""
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"mount -o remount,rw /media/root-ro && "
             f"cp {_OGLE_SERVICE_FILE} {_SD_OGLE_SERVICE} && "
             f"chmod 644 {_SD_OGLE_SERVICE} && "
             f"sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return jsonify(ok=False, error=r.stderr.strip() or "persist failed"), 500
        md5_r = subprocess.run(
            ["bash", "-c", f"md5sum {_OGLE_SERVICE_FILE} | awk '{{print $1}}'"],
            capture_output=True, text=True, timeout=5)
        md5_val = md5_r.stdout.strip()
        match_r = subprocess.run(
            ["bash", "-c",
             f"md5sum {_OGLE_SERVICE_FILE} {_SD_OGLE_SERVICE} "
             f"| awk '{{print $1}}' | sort -u | wc -l"],
            capture_output=True, text=True, timeout=5)
        verified = match_r.stdout.strip() == "1"
        return jsonify(ok=verified, verified=verified, md5=md5_val)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ── Clips (WS2A S158) ────────────────────────────────────────────────────────
CLIPS_DIR    = "/home/pi/clips"
SD_CLIPS_DIR = "/media/root-ro/home/pi/clips"


@app.route("/api/clips")
def api_clips():
    """List WAV files in /home/pi/clips/ with filename, size, and trigger keywords.
    Reads iris_soundboard.json fresh so keywords are current after a Soundboard save."""
    try:
        from core.soundboard import get_clips as _sb_get_clips
        trigger_map = {e["file"]: e.get("triggers", []) for e in _sb_get_clips(False)}
    except Exception:
        trigger_map = {}
    try:
        files = []
        if os.path.isdir(CLIPS_DIR):
            for fn in sorted(os.listdir(CLIPS_DIR)):
                if fn.lower().endswith(".wav"):
                    path = os.path.join(CLIPS_DIR, fn)
                    files.append({
                        "filename": fn,
                        "size":     os.path.getsize(path),
                        "keywords": trigger_map.get(fn, []),
                    })
        return jsonify(clips=files)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/clips/play/<filename>", methods=["POST"])
def api_clips_play(filename):
    """Non-blocking aplay of a named clip from the clips directory."""
    if "/" in filename or "\\" in filename or not filename.lower().endswith(".wav"):
        return jsonify(ok=False, error="invalid filename"), 400
    path = os.path.join(CLIPS_DIR, filename)
    if not os.path.isfile(path):
        return jsonify(ok=False, error="not found"), 404
    try:
        subprocess.Popen(["aplay", "-D", "plug:dmixed", "-q", path])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/clips/upload", methods=["POST"])
def api_clips_upload():
    """Accept a WAV upload and write atomically to RAM and SD with md5 verification.
    Returns 200 only when both paths are written and md5-verified. 500 on any failure."""
    import hashlib, shlex
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="no file in request"), 400
    fn = os.path.basename(f.filename or "")
    if not fn.lower().endswith(".wav"):
        return jsonify(ok=False, error="WAV files only"), 400
    if len(fn) > 200:
        return jsonify(ok=False, error="filename too long"), 400
    fn = re.sub(r'[^A-Za-z0-9._\- ]', '_', fn)
    data = f.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        return jsonify(ok=False, error="file exceeds 2 MB limit"), 400
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return jsonify(ok=False, error="not a valid WAV file"), 400
    upload_md5 = hashlib.md5(data).hexdigest()
    ram_path = os.path.join(CLIPS_DIR, fn)
    sd_path  = os.path.join(SD_CLIPS_DIR, fn)
    tmp_path = os.path.join(CLIPS_DIR, ".upload_tmp")
    # Collision guard: never overwrite an existing clip in this session.
    if os.path.exists(ram_path):
        return jsonify(ok=False, error="already exists"), 409
    try:
        os.makedirs(CLIPS_DIR, exist_ok=True)
        # Atomic RAM write: temp file → md5-verify → os.replace() into final path.
        # A failed write cannot clobber an existing clip (it never touches ram_path).
        with open(tmp_path, "wb") as wf:
            wf.write(data)
        with open(tmp_path, "rb") as rf:
            tmp_md5 = hashlib.md5(rf.read()).hexdigest()
        if tmp_md5 != upload_md5:
            try: os.unlink(tmp_path)
            except Exception: pass
            return jsonify(ok=False, error="RAM write md5 mismatch"), 500
        os.replace(tmp_path, ram_path)
        # Write to SD via overlayfs remount (same pattern as api_ps_config_persist).
        # Quote every path arg into bash -c with shlex.quote; the mount/unmount
        # sequence itself is unchanged (per HANDOFF PREVENTION RULES).
        q_sd_dir = shlex.quote(SD_CLIPS_DIR)
        q_ram    = shlex.quote(ram_path)
        q_sd     = shlex.quote(sd_path)
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"mkdir -p {q_sd_dir} && "
             f"mount -o remount,rw /media/root-ro && "
             f"cp {q_ram} {q_sd} && "
             f"chmod 644 {q_sd} && sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return jsonify(ok=False, error=f"SD write failed: {r.stderr.strip() or 'unknown'}"), 500
        # Verify SD write matches RAM
        match_r = subprocess.run(
            ["bash", "-c",
             f"md5sum {q_ram} {q_sd} | awk '{{print $1}}' | sort -u | wc -l"],
            capture_output=True, text=True, timeout=5)
        if match_r.stdout.strip() != "1":
            return jsonify(ok=False, error="SD md5 mismatch after write"), 500
        return jsonify(ok=True, filename=fn, md5=upload_md5)
    except Exception as e:
        try: os.unlink(tmp_path)
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


# ── Bench Recent / Turn Latency (RD-007 S158) ────────────────────────────────

@app.route("/api/bench_recent")
def api_bench_recent():
    """Return last 20 entries from iris_bench.jsonl as JSON array.
    Fields: ts, stt_ms, llm_ms, tts_ms, total_ms, route, emotion, cold.
    Returns empty list if file is missing (not 500)."""
    try:
        from core.config import BENCH_LOG as _BL
    except Exception:
        _BL = "/home/pi/logs/iris_bench.jsonl"
    out = []
    try:
        with open(_BL, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        for line in lines[-20:]:
            try:
                rec = json.loads(line)
                st = rec.get("stages", {})
                out.append({
                    "ts":      rec.get("ts", ""),
                    "stt_ms":  st.get("stt_ms"),
                    "llm_ms":  st.get("llm_total_ms"),
                    "tts_ms":  st.get("tts_ms"),
                    "total_ms": rec.get("total_ms") or st.get("play_start_ms"),
                    "route":   rec.get("route", ""),
                    "emotion": rec.get("emotion"),
                    "cold":    rec.get("gandalf_was_cold", False),
                })
            except Exception:
                pass
    except FileNotFoundError:
        pass
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(entries=out)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
