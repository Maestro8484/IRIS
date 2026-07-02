"""IRIS log parsing — journal + SD daily log events."""
import os, re as _re, glob as _glob

_SD_LOG_DIR   = "/media/root-ro/home/pi/logs"
_SD_MONTH_MAP = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                 'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

_BENCH_RE     = _re.compile(r'\[BENCH\](.*)')
_TS_RE        = _re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
# Fixed: journal uses python3[PID]: not assistant[PID]:
_MSG_RE       = _re.compile(r'(?:python3|assistant|iris[\-_]web)\[\d+\]:\s*(.*)')
_TAG_RE       = _re.compile(
    r'\[(INFO|ERR|WARN|STT|LLM|TTS|ROUTE|WAKEWORD|STOP|CMD|GESTURE)\](.*)')
_GESTURE_RE   = _re.compile(r'\[GESTURE\]\s+gesture=(\S+)\s+action=(\S+)')
_BASE_GEST_RE = _re.compile(r'\[BASE\]\s+(VOL[+\-]|STOP|LISTEN|FORWARD|BACKWARD|CW|CCW)\s*$')
# SD daily log line: "May 22 17:30:00 pi4-satellite python3[1139]: message"
_SD_LINE_RE   = _re.compile(
    r'^(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+\S+\s+'
    r'(?:python3|assistant|iris[\-_]web)\[\d+\]:\s*(.*)')
_DRIFT_SIGNALS = ("certainly", "of course", "i'd be happy", "as an ai",
                  "great question", "i cannot help with", "i apologize",
                  "i'm sorry, but", "as an assistant", "i understand that")


def _kv(s):
    d = {}
    for p in s.split():
        if '=' in p:
            k, _, v = p.partition('=')
            d[k] = v.strip('"\'')
    return d


def _parse_event_msg(ts, ts_s, msg):
    """Parse a log message into an event dict, or return None if not interesting."""
    bm = _BENCH_RE.search(msg)
    if bm:
        kv    = _kv(bm.group(1))
        stage = kv.get("stage", "")
        ev    = {"t": ts, "ts": ts_s, "detail": ""}
        if stage == "wake_detected":
            ev.update(cat="wakeword", msg="Wakeword detected",
                      detail=f'trigger={kv.get("trigger","?")}')
        elif stage == "stt_done":
            tx = kv.get("transcript", "")
            ev.update(cat="stt", msg=f'Heard: "{tx}"',
                      detail=f'STT {kv.get("dur_stt","?")}s')
        elif stage == "llm_start":
            ev.update(cat="route", msg=f'→ LLM  tier={kv.get("tier","?")}',
                      detail=f'np={kv.get("num_predict","?")}')
        elif stage == "llm_first_chunk":
            ev.update(cat="llm", msg="First LLM chunk",
                      detail=f'ttfc={kv.get("dur_ttfc","?")}s')
        elif stage == "llm_done":
            ev.update(cat="llm", msg="LLM response complete",
                      detail=f'{kv.get("dur_llm","?")}s')
        elif stage == "tts_done":
            ev.update(cat="tts", msg="TTS synthesized",
                      detail=f'{kv.get("dur_tts","?")}s')
        elif stage == "audio_done":
            ev.update(cat="tts", msg=f'Spoken — total {kv.get("dur_total","?")}s')
        elif stage == "ollama_stats":
            ev.update(cat="llm",
                      msg=f'Tokens: eval={kv.get("eval_tokens","?")} prompt={kv.get("prompt_tokens","?")}')
        else:
            return None
        return ev

    gm = _GESTURE_RE.search(msg)
    if gm:
        gesture, action = gm.group(1), gm.group(2)
        label = f"{gesture} -> {action}" if action != gesture else gesture
        return {"t": ts, "ts": ts_s, "cat": "gesture",
                "msg": f"Gesture: {label}", "detail": ""}

    bsm = _BASE_GEST_RE.search(msg)
    if bsm:
        return {"t": ts, "ts": ts_s, "cat": "gesture",
                "msg": f"Gesture: {bsm.group(1)}", "detail": ""}

    tm = _TAG_RE.search(msg)
    if tm:
        tag     = tm.group(1)
        content = tm.group(2).strip()
        cat_map = {"INFO": "info", "ERR": "error", "WARN": "warn", "STT": "stt",
                   "LLM": "llm", "TTS": "tts", "ROUTE": "route",
                   "WAKEWORD": "wakeword", "STOP": "stop", "CMD": "cmd",
                   "GESTURE": "gesture"}
        cat = cat_map.get(tag, "info")
        if any(p in content.lower() for p in _DRIFT_SIGNALS):
            return {"t": ts, "ts": ts_s, "cat": "drift",
                    "msg": "DRIFT: boilerplate/formal opener",
                    "detail": content[:120]}
        if tag in ("STOP", "INFO") and ("stop phrase" in content.lower()
                   or "stop gate" in content.lower() or "[stop]" in content.lower()):
            cat = "stop"
        return {"t": ts, "ts": ts_s, "cat": cat,
                "msg": content[:140], "detail": ""}

    lower = msg.lower()
    if "stop phrase" in lower or "stop gate" in lower:
        return {"t": ts, "ts": ts_s, "cat": "stop",
                "msg": msg[:140], "detail": ""}
    return None


def _sd_events(n_days=3650):
    """Parse SD daily assistant log files into event dicts (history across reboots)."""
    results = []
    files = sorted(_glob.glob(os.path.join(_SD_LOG_DIR, "iris-????????.log")))[-n_days:]
    for fpath in files:
        fname = os.path.basename(fpath)
        try:
            year = int(fname[5:9])
        except (ValueError, IndexError):
            continue
        try:
            with open(fpath, errors="ignore") as f:
                for line in f:
                    m = _SD_LINE_RE.match(line.rstrip())
                    if not m:
                        continue
                    mon_s, day_s, hh, mm, ss, msg = m.groups()
                    mo = _SD_MONTH_MAP.get(mon_s, 0)
                    if not mo:
                        continue
                    try:
                        ts   = f"{year}-{mo:02d}-{int(day_s):02d}T{hh}:{mm}:{ss}"
                        ts_s = f"{hh}:{mm}:{ss}"
                    except Exception:
                        continue
                    ev = _parse_event_msg(ts, ts_s, msg.strip())
                    if ev:
                        results.append(ev)
        except Exception:
            continue
    return results
