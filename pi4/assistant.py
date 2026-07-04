#!/usr/bin/env python3
"""
assistant.py - Pi4 IRIS voice assistant
Wake: wyoming-openwakeword hey_jarvis (:10400) OR button press (GPIO17)
STT:  Wyoming Whisper  @ 192.168.1.3:10300
LLM:  Ollama           @ 192.168.1.3:11434 (streaming)
TTS:  Kokoro           @ 192.168.1.3:8004 (primary) / Piper @ :10200 (fallback)
Audio: wm8960-soundcard (dynamic card detection)
LEDs: 3x APA102 via SPI -- status indicator
Eyes: Teensy 4.1 via /dev/ttyIRIS_EYES
Base: Teensy 4.0 servo/gesture via /dev/ttyIRIS_SERVO (BaseMountBridge)
"""

import json, os, queue, re, socket, subprocess, sys, threading, time
import numpy as np
import pyaudio
import requests
import warnings; warnings.filterwarnings("ignore")

from core.config import *
from hardware.teensy_bridge import TeensyBridge
from hardware.base_mount_bridge import BaseMountBridge
from hardware.led import APA102
from hardware.io import setup_button, button_pressed, gpio_cleanup
from hardware.audio_io import (
    _find_mic_device_index, get_volume, set_volume, handle_volume_command,
    play_pcm, play_pcm_speaking, play_pcm_stream, play_beep, play_double_beep, play_wol_beep,
    record_command, _stop_playback, STOP_PHRASES, FOLLOWUP_DISMISSALS,
)
from services.wyoming import wy_send, read_line
from services.stt import transcribe
from services.tts import synthesize, spoken_numbers
from services.llm import stream_ollama, classify_response_length
from services.vision import (
    capture_image, is_vision_trigger, ask_vision, ask_vision_game, classify_camera_game,
)
from services.wakeword import wait_for_wakeword_or_button
from state.state_manager import state
from core.intent_router import (
    IntentRouter, IntentResult,
    ROUTE_REFLEX, ROUTE_COMMAND, ROUTE_UTILITY, ROUTE_AMBIGUOUS, ROUTE_LLM,
)
from core.clip_triggers import check_clip_trigger
from core.clip_player import play_clip


def get_model() -> str:
    return OLLAMA_MODEL_KIDS if state.kids_mode else OLLAMA_MODEL_ADULT


# ── Quips (data-driven via core/soundboard.py, S163) ──────────────────────────
# The wake / double-tap / post-speech / kids-filler / gesture-cue quips are
# seeded in core/soundboard.py (verbatim from the prior hardcoded lists) and
# edited live from the WebUI Soundboard tab. We build the in-memory structures
# the rest of this module already expects from soundboard.get_quips(). A disabled
# category yields an empty set; disabled wake bands are dropped (the hour then
# falls through to the default in _pick_wake_quip). reload_soundboard() rebuilds
# these and re-runs _pre_synthesize_quips() after a WebUI save (no restart).
from core import soundboard


def _build_quip_structs():
    """Return (wake, double_tap, post_speech, kids, gesture) from the soundboard.
    wake: list of (h_start, h_end, emotion, [lines]) for ENABLED bands only.
    double_tap / post_speech / kids: line lists (empty if category disabled).
    gesture: {key: phrase} (empty if disabled)."""
    try:
        q = soundboard.get_quips()
    except Exception as _e:
        print(f"[QUIP] soundboard load failed, using empty quips: {_e}", flush=True)
        q = {}
    wake = [
        (b["hour_start"], b["hour_end"], b.get("emotion", "NEUTRAL"),
         list(b.get("lines", [])))
        for b in q.get("wake", []) if b.get("enabled", True)
    ]

    def _lines(cat):
        c = q.get(cat) or {}
        return list(c.get("lines", [])) if c.get("enabled", True) else []

    dbl_obj  = q.get("double_tap") or {}
    post_obj = q.get("post_speech") or {}
    dbl  = _lines("double_tap")
    post = _lines("post_speech")
    kids = _lines("kids_fillers")
    gc_obj  = q.get("gesture_cues") or {}
    gesture = dict(gc_obj.get("cues", {})) if gc_obj.get("enabled", True) else {}
    # Bundle the lower-frequency config (top-of-hour, first-of-day, retort
    # emotions, RPQR timing) so the module unpack stays small and the RPQR
    # cascade reads it directly.
    qcfg = {
        "top_of_hour":     q.get("top_of_hour") or {},
        "first_of_day":    q.get("first_of_day") or {},
        "double_tap_emo":  dbl_obj.get("emotion", "AMUSED"),
        "post_speech_emo": post_obj.get("emotion", "AMUSED"),
        "timing":          q.get("rpqr_timing") or {},
    }
    return wake, dbl, post, kids, gesture, qcfg


(_WAKE_QUIPS, _DOUBLE_TAP_QUIPS, _POST_SPEECH_QUIPS,
 _KIDS_THINK_FILLERS, _GESTURE_CUES, _QCFG) = _build_quip_structs()
_kids_filler_cache: dict = {}
_gesture_cue_cache: dict = {}

# Set while a streaming LLM turn is actively playing audio, so gesture cues
# skip the speaker (LED + the action itself are feedback enough mid-speech).
_tts_active = threading.Event()

# Last resolved emotion from any completed LLM turn — used by clip trigger
# to select affective variants before the current turn's emotion is known.
_last_known_emotion: str = "NEUTRAL"

_HOUR_NAMES = [
    "Midnight", "One", "Two", "Three", "Four", "Five", "Six",
    "Seven", "Eight", "Nine", "Ten", "Eleven", "Noon",
    "One", "Two", "Three", "Four", "Five", "Six",
    "Seven", "Eight", "Nine", "Ten", "Eleven",
]


def _toh_line_for(hour: int) -> str:
    """Top-of-hour spoken line for `hour` (0-23) from the data-driven config
    (_QCFG["top_of_hour"]). A per-hour override wins; otherwise the template is
    filled with the hour name from _HOUR_NAMES. Falls back to the original
    verbatim phrasing if the template is unusable."""
    h = hour % 24
    cfg = _QCFG.get("top_of_hour", {})
    ov = cfg.get("overrides") or {}
    line = ov.get(str(h))
    if isinstance(line, str) and line.strip():
        return line
    name = _HOUR_NAMES[h]
    tmpl = cfg.get("template") or "{hour} o'clock. That's the whole thought."
    try:
        return tmpl.format(hour=name)
    except (KeyError, IndexError, ValueError):
        return f"{name} o'clock. That's the whole thought."


def _first_of_day_line(hour: int) -> str:
    """First-interaction-of-the-day line from _QCFG["first_of_day"]: the morning
    line before cutoff_hour, the evening line at/after it. Falls back to the
    original verbatim phrasing."""
    cfg = _QCFG.get("first_of_day", {})
    try:
        cutoff = int(cfg.get("cutoff_hour", 9))
    except (TypeError, ValueError):
        cutoff = 9
    if hour < cutoff:
        line = cfg.get("morning")
        return line if isinstance(line, str) and line.strip() else "Morning."
    line = cfg.get("evening")
    return line if isinstance(line, str) and line.strip() else "Finally."

_wake_quip_cache: dict = {}
_rpqr_cache: dict = {}
_game_intro_cache: dict = {}
_last_quip_line: str = ""

# mutable state dict — avoids global declarations in main loop
_rpqr_state: dict = {
    "t_last_wake":          0.0,
    "t_last_spoke":         0.0,
    "last_interaction_date": None,
    "t_last_top_of_hour":   0.0,
}

# Camera-game cadence state (S168). `active` is True while a reciprocal camera
# game (I Spy / Show Me / Face) is mid-flow so the follow-up loop keeps offering
# turns, the double-beep is suppressed, and the RPQR wake-quip cascade is muted.
# `t_ended` stamps a clean game exit so a follow-up wakeword within
# GAME_REENTRY_GRACE_S still skips the snarky quip cascade.
_cam_game_state: dict = {
    "active":          False,
    "game":            None,
    "turns_remaining": 0,
    "t_ended":         0.0,
}


def _pick_wake_quip(hour: int) -> tuple:
    import random
    global _last_quip_line
    for h_start, h_end, emotion, lines in _WAKE_QUIPS:
        if h_start <= hour < h_end:
            if not lines:        # band enabled but emptied in the UI: skip it so
                continue         # another band or the fallback handles the hour
            choices = [l for l in lines if l != _last_quip_line] or lines
            line = random.choice(choices)
            _last_quip_line = line
            return line, emotion
    return "Yeah.", "NEUTRAL"


def _play_wake_quip(hour: int, pa, teensy, leds) -> None:
    line, emotion = _pick_wake_quip(hour)
    pcm = _wake_quip_cache.get(line)
    if not pcm:
        print(f"[QUIP] No cache for '{line}' -- skipping", flush=True)
        return
    try:
        emit_emotion(teensy, leds, emotion)
        play_pcm_speaking(pcm, pa, teensy, restore_mouth_idx=0)
        print(f"[QUIP] {emotion}: {line!r}", flush=True)
    except Exception as _e:
        print(f"[QUIP] Failed: {_e}", flush=True)


def _play_rpqr(line: str, emotion: str, pa, teensy, leds) -> None:
    pcm = _rpqr_cache.get(line)
    if not pcm:
        print(f"[RPQR] No cache for '{line}' -- skipping", flush=True)
        return
    try:
        emit_emotion(teensy, leds, emotion)
        play_pcm_speaking(pcm, pa, teensy, restore_mouth_idx=0)
        print(f"[RPQR] {emotion}: {line!r}", flush=True)
    except Exception as _e:
        print(f"[RPQR] Failed: {_e}", flush=True)


def _pre_synthesize_quips() -> None:
    from core.config import KOKORO_SPEED_QUIPS
    unique_wake = {l for _, _, _, lines in _WAKE_QUIPS for l in lines}
    unique_wake.add("Yeah.")   # _pick_wake_quip fallback (no/empty band for hour)
    for line in unique_wake:
        try:
            _wake_quip_cache[line] = synthesize(line, speed=KOKORO_SPEED_QUIPS)
            print(f"[QUIP] Cached: {line!r}", flush=True)
        except Exception as _e:
            print(f"[QUIP] Cache miss '{line}': {_e}", flush=True)

    rpqr_lines: list = list(_DOUBLE_TAP_QUIPS) + list(_POST_SPEECH_QUIPS)
    _fod = _QCFG.get("first_of_day", {})
    if _fod.get("enabled", True):
        for _k in ("morning", "evening"):
            _fl = _fod.get(_k)
            if isinstance(_fl, str) and _fl.strip():
                rpqr_lines.append(_fl)
    if _QCFG.get("top_of_hour", {}).get("enabled", True):
        seen_toh: set = set()
        for h in range(24):
            toh = _toh_line_for(h)
            if toh and toh not in seen_toh:
                rpqr_lines.append(toh)
                seen_toh.add(toh)
    for line in rpqr_lines:
        try:
            _rpqr_cache[line] = synthesize(line, speed=KOKORO_SPEED_QUIPS)
            print(f"[RPQR] Cached: {line!r}", flush=True)
        except Exception as _e:
            print(f"[RPQR] Cache miss '{line}': {_e}", flush=True)

    # Kids gap-fillers (cached regardless of current mode -- kids mode can be
    # toggled at runtime).
    for line in _KIDS_THINK_FILLERS:
        try:
            _kids_filler_cache[line] = synthesize(line, speed=KOKORO_SPEED_QUIPS)
            print(f"[KIDFILL] Cached: {line!r}", flush=True)
        except Exception as _e:
            print(f"[KIDFILL] Cache miss '{line}': {_e}", flush=True)

    # Gesture audible cues.
    for key, phrase in _GESTURE_CUES.items():
        try:
            _gesture_cue_cache[key] = synthesize(phrase, speed=KOKORO_SPEED_QUIPS)
            print(f"[GCUE] Cached: {key}={phrase!r}", flush=True)
        except Exception as _e:
            print(f"[GCUE] Cache miss '{key}': {_e}", flush=True)

    # Camera-game intro lines (S168 Break 1) -- masks capture+vision latency.
    for _gk, _gp in _CAMERA_GAME_INTROS.items():
        try:
            _game_intro_cache[_gk] = synthesize(_gp, speed=KOKORO_SPEED_QUIPS)
            print(f"[GINTRO] Cached: {_gk}={_gp!r}", flush=True)
        except Exception as _e:
            print(f"[GINTRO] Cache miss '{_gk}': {_e}", flush=True)


def reload_soundboard() -> None:
    """Re-read the soundboard after a WebUI save (CMD RELOAD_SOUNDBOARD): refresh
    the enabled clip set and rebuild + re-synthesize all quip caches in-process,
    so edits take effect without a service restart. Runs in the CMD listener
    thread; best-effort, never fatal."""
    global _WAKE_QUIPS, _DOUBLE_TAP_QUIPS, _POST_SPEECH_QUIPS
    global _KIDS_THINK_FILLERS, _GESTURE_CUES, _QCFG
    try:
        soundboard.reload()
    except Exception as _e:
        print(f"[SOUNDBOARD] data reload failed: {_e}", flush=True)
    try:
        from core import clip_triggers
        _n = clip_triggers.reload()
        print(f"[SOUNDBOARD] clips reloaded: {_n} active", flush=True)
    except Exception as _e:
        print(f"[SOUNDBOARD] clip reload failed: {_e}", flush=True)
    try:
        (_WAKE_QUIPS, _DOUBLE_TAP_QUIPS, _POST_SPEECH_QUIPS,
         _KIDS_THINK_FILLERS, _GESTURE_CUES, _QCFG) = _build_quip_structs()
        _wake_quip_cache.clear()
        _rpqr_cache.clear()
        _kids_filler_cache.clear()
        _gesture_cue_cache.clear()
        _pre_synthesize_quips()
        print("[SOUNDBOARD] quips rebuilt + re-synthesized", flush=True)
    except Exception as _e:
        print(f"[SOUNDBOARD] quip reload failed: {_e}", flush=True)


# ── Conversation logger ───────────────────────────────────────────────────────

def flush_conversation_log(reason: str = "timeout"):
    if not state.conversation_history:
        return
    import datetime
    os.makedirs(os.path.dirname(CONVERSATION_LOG), exist_ok=True)
    record = {
        "ts":       datetime.datetime.now().isoformat(timespec="seconds"),
        "reason":   reason,
        "mode":     "kids" if state.kids_mode else "adult",
        "model":    get_model(),
        "turns":    sum(1 for m in state.conversation_history if m["role"] == "user"),
        "messages": list(state.conversation_history),
    }
    try:
        with open(CONVERSATION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[LOG]  Session logged ({record['turns']} turns, reason={reason})", flush=True)
    except Exception as e:
        print(f"[ERR]  Failed to write conversation log: {e}", flush=True)


# ── Context timeout watchdog ──────────────────────────────────────────────────

def _context_watchdog():
    if CONTEXT_TIMEOUT_SECS <= 0:
        return
    while True:
        time.sleep(30)
        if state.last_interaction == 0.0:
            continue
        elapsed = time.time() - state.last_interaction
        if elapsed >= CONTEXT_TIMEOUT_SECS and state.conversation_history:
            flush_conversation_log(reason="timeout")
            state.clear_conversation()
            state.last_interaction = 0.0
            print(f"[CTX]  Context cleared after {CONTEXT_TIMEOUT_SECS}s of silence", flush=True)
        if state.kids_mode and elapsed >= KIDS_MODE_INACTIVITY_TIMEOUT:
            state.kids_mode = False
            flush_conversation_log(reason="kids_mode_timeout")
            state.clear_conversation()
            print(f"[MODE] Kids mode auto-off after {KIDS_MODE_INACTIVITY_TIMEOUT}s inactivity", flush=True)


# ── WoL + GandalfAI readiness ─────────────────────────────────────────────────

def send_wol(mac: str, ip: str = "255.255.255.255", port: int = 9):
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, (ip, port))
    print(f"[WOL]  Magic packet sent to {mac} via {ip}:{port}", flush=True)


def gandalf_is_up() -> bool:
    try:
        with socket.create_connection((GANDALF, OLLAMA_PORT), timeout=3):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def ensure_gandalf_up(leds, pa=None) -> bool:
    if gandalf_is_up():
        return True
    print("[WOL]  GandalfAI is offline -- sending Wake-on-LAN...", flush=True)
    send_wol(GANDALF_MAC, GANDALF_WOL_IP, GANDALF_WOL_PORT)
    if pa is not None:
        play_wol_beep(pa)

    leds.show_wol()
    deadline = time.monotonic() + WOL_BOOT_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(WOL_POLL_INTERVAL)
        if gandalf_is_up():
            leds.stop_anim()
            print("[WOL]  GandalfAI is up.", flush=True)
            return True
        print(f"[WOL]  Waiting for GandalfAI... ({int(deadline-time.monotonic())}s remaining)", flush=True)
    leds.stop_anim()
    print("[ERR]  GandalfAI did not come up in time.", flush=True)
    return False


# ── CMD listener + Emotion helper ─────────────────────────────────────────────

def _play_gesture_cue(token, pa, teensy):
    """Acknowledge a gesture with a TFT mouth pulse + short spoken cue.
    Skips the speaker while a streaming LLM turn is talking (the LED flash and
    the action itself are feedback enough mid-speech). Best-effort, non-fatal."""
    try:
        if GESTURE_MOUTH_CUE and not _tts_active.is_set():
            # Brief SILLY-face gesture pulse, then restore to NEUTRAL. Frame 9
            # (SILLY) reads as a fun "got it!" ack. Works on current firmware;
            # S144 firmware also accepts a crisper native "MOUTHGEST" command.
            def _pulse():
                try:
                    teensy.send_command("MOUTH:9")   # SILLY
                    time.sleep(0.5)
                    teensy.send_command("MOUTH:0")   # NEUTRAL
                except Exception:
                    pass
            threading.Thread(target=_pulse, daemon=True).start()
        if GESTURE_AUDIO_CUE and not _tts_active.is_set():
            pcm = _gesture_cue_cache.get(token)
            if pcm:
                play_pcm(pcm, pa)
                print(f"[GCUE] {token}", flush=True)
    except Exception as e:
        print(f"[GCUE] error: {e}", flush=True)


def start_cmd_listener(teensy, leds, pa=None):
    """UDP listener on CMD_PORT. iris_web.py sends raw commands here."""
    def _listener():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", CMD_PORT))
            print(f"[CMD] Listening for web UI commands on UDP port {CMD_PORT}", flush=True)
            while True:
                try:
                    data, _ = s.recvfrom(256)
                    cmd = data.decode(errors="ignore").strip()
                    if cmd:
                        if cmd in ("STOP_PLAYBACK", "STOP"):
                            _stop_playback.set()
                            print(f"[CMD] {cmd}: playback interrupted", flush=True)
                        elif cmd == "RELOAD_SOUNDBOARD":
                            print("[CMD] RELOAD_SOUNDBOARD", flush=True)
                            reload_soundboard()
                        elif cmd.startswith("GCUE:"):
                            # Gesture acknowledgment from the base mount bridge.
                            _play_gesture_cue(cmd[5:].strip(), pa, teensy)
                        else:
                            # RD-033: GAZE: arrives at the OGLE frame rate; don't log
                            # it per-packet (RD-031). The teensy_bridge >> echo behind
                            # IRIS_DEBUG_SERIAL=1 is the debug surface for gaze traffic.
                            if not cmd.startswith("GAZE:"):
                                print(f"[CMD] -> teensy: {cmd}", flush=True)
                            if state.eyes_sleeping and (
                                cmd.startswith("EMOTION:") or cmd.startswith("EYE:")
                                or cmd.startswith("MOUTH:")
                            ):
                                _do_wake(teensy, leds)
                                print(f"[CMD] Auto-woke eyes for: {cmd}", flush=True)
                            teensy.send_command(cmd)
                            if cmd == "EYES:SLEEP":
                                _do_sleep(teensy, leds)
                            elif cmd == "EYES:WAKE":
                                _do_wake(teensy, leds)
                except Exception as e:
                    print(f"[CMD] Listener error: {e}", flush=True)
    threading.Thread(target=_listener, daemon=True).start()


def emit_emotion(teensy, leds, emotion: str):
    """Send emotion to Teensy eyes AND sync LED color in one call."""
    eye_idx = EMOTION_EYE_MAP.get(emotion, -1)
    if eye_idx >= 0:
        teensy.send_command(f"EYE:{eye_idx}")
    teensy.send_emotion(emotion)
    teensy.send_command(f"MOUTH:{MOUTH_MAP.get(emotion, 0)}")
    leds.show_emotion(emotion)


# ── Local command handlers ────────────────────────────────────────────────────

def handle_kids_mode_command(text: str):
    t = text.lower().strip().rstrip(".!?")
    on_triggers  = ("kids mode on", "enable kids mode", "turn on kids mode", "switch to kids mode",
                    "kids mode please", "activate kids mode", "children's mode on", "kid mode on")
    off_triggers = ("kids mode off", "kids mode stop", "disable kids mode", "turn off kids mode",
                    "deactivate kids mode", "kid mode off",
                    "exit kids mode", "leave kids mode", "stop kids mode", "quit kids mode",
                    "end kids mode", "no more kids mode",
                    "adult mode", "normal mode", "grown up mode", "grownup mode", "big kid mode",
                    "back to normal", "be normal", "talk normal")
    if any(tr in t for tr in on_triggers):
        state.kids_mode = True
        flush_conversation_log(reason="mode_switch_kids_on")
        state.clear_conversation()
        print(f"[MODE] Kids mode ON -- model: {OLLAMA_MODEL_KIDS}", flush=True)
        return "Kids mode activated.", True
    if any(tr in t for tr in off_triggers):
        state.kids_mode = False
        flush_conversation_log(reason="mode_switch_kids_off")
        state.clear_conversation()
        print(f"[MODE] Kids mode OFF -- model: {OLLAMA_MODEL_ADULT}", flush=True)
        return "Kids mode deactivated.", False
    return None, None


def handle_time_command(text: str):
    t = text.lower().strip().rstrip(".!?")
    time_triggers = ("what time", "what's the time", "whats the time", "current time",
                     "tell me the time", "time is it", "what hour")
    date_triggers = ("what day", "what date", "what's the date", "whats the date",
                     "today's date", "todays date", "what month", "what year", "day is it", "date is it")
    is_time = any(tr in t for tr in time_triggers)
    is_date = any(tr in t for tr in date_triggers)
    if not (is_time or is_date): return None
    now = time.localtime()
    hour = now.tm_hour; minute = now.tm_min
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    if minute == 0: time_str = f"{hour12} {period}"
    elif minute < 10: time_str = f"{hour12} oh {minute} {period}"
    else: time_str = f"{hour12} {minute} {period}"
    day_name   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][now.tm_wday]
    month_name = ["January","February","March","April","May","June","July",
                  "August","September","October","November","December"][now.tm_mon - 1]
    if is_time and is_date: return f"It is {time_str} on {day_name}, {month_name} {now.tm_mday}."
    elif is_time: return f"It is {time_str}."
    else: return f"Today is {day_name}, {month_name} {now.tm_mday}, {now.tm_year}."



# ── LLM helpers ───────────────────────────────────────────────────────────────

def _build_messages() -> list:
    """Build the messages list for Ollama.

    The IRIS persona lives ENTIRELY in the modelfile SYSTEM prompt. Do NOT send a
    {"role":"system"} message here: in Ollama /api/chat a request system message
    OVERRIDES the modelfile SYSTEM, which strips the persona and leaves a generic
    apologetic assistant. Proven S134 -- a date-only system message turned
    "[EMOTION:ANGRY] Frustration noted. Get it right next time." into
    "I'm sorry to hear that you're frustrated. I'm here to help...". That was the
    "as an AI assistant" / apology-under-insult regression.

    Date context is instead folded into the CURRENT (latest) user turn -- which the
    persona is told to expect ("Date and time context may be provided to you") --
    on a COPY, so stored conversation_history keeps the raw user text.
    """
    import datetime
    now = datetime.datetime.now()
    msgs = [dict(m) for m in state.conversation_history]
    stamp = f"(Context, not spoken: it is {now.strftime('%A, %B %d %Y, %I:%M %p')} Mountain Time.) "
    for m in reversed(msgs):
        if m.get("role") == "user":
            m["content"] = stamp + m["content"]
            break
    return msgs


def _speak_llm_turn(text, num_predict, teensy, leds, pa, mic,
                    bench_stages, t_mono_wake, gandalf_was_cold=False,
                    stage_prefix=""):
    """
    One streaming LLM turn, shared by the main turn and the follow-up loop:
    stream_ollama() yields cleaned sentence chunks (emotion on the first); each
    sentence is synthesized as it arrives and queued to a background
    play_pcm_stream player that plays blobs back-to-back, so first audio starts
    on the first sentence while later sentences are still being generated and
    synthesized. STOP is checked per sentence dispatch, not just at end.

    - Appends the user text to conversation history up front and the assistant
      reply at the end (history trimmed at 20 messages) -- same contract the
      blocking ask_ollama() had before it was retired (S126).
    - synthesize() does Kokoro->Piper fallback internally; a sentence is
      skipped (not fatal) if both engines fail.
    - Cumulative TTS_MAX_CHARS cap across the whole utterance (S122).
    - Producer owns the _stop_playback lifecycle: cleared at turn start and
      turn end (play_pcm_stream never clears it -- see S122).
    - Bench stages land in bench_stages; the caller owns _bench_write().
      stage_prefix namespaces the journal [BENCH] stage names (follow-up turns
      pass "fu_" so /api/bench's journal parser doesn't overwrite the main
      turn's stages within the same wake cycle).
    - mic is stopped when playback starts; the caller owns restarting it.
    - _rpqr_state["t_last_spoke"] is stamped after playback drains.

    Returns (reply, emotion, interrupted, ok). ok=False means the stream
    failed before any audio started; the user message is already in history
    and the caller decides recovery (error LED for the main turn, break for
    the follow-up loop).
    """
    _tier = {NUM_PREDICT_SHORT: "SHORT", NUM_PREDICT_MEDIUM: "MEDIUM",
             NUM_PREDICT_LONG: "LONG", NUM_PREDICT_MAX: "MAX"}.get(num_predict, "CUSTOM")
    print(f"[LLM]  Streaming... (model={get_model()}, num_predict={num_predict})", flush=True)
    _t_llm0 = time.time()
    _t_mono_llm0 = time.monotonic()
    _t_mono_llm_first = _t_mono_llm0
    _t_llm_first = _t_llm0
    try:
        bench_stages["tier"] = _tier
        bench_stages["num_predict"] = num_predict
        print(f"[BENCH] t={_t_llm0:.3f} stage={stage_prefix}llm_start tier={_tier} num_predict={num_predict} model={get_model()} gandalf_was_cold={str(gandalf_was_cold).lower()}", flush=True)
    except Exception:
        pass
    state.last_interaction = time.time()
    state.conversation_history.append({"role": "user", "content": text})

    reply_parts = []
    _interrupted = False
    _emotion_set = False
    _current_emotion = "NEUTRAL"
    _bench_first_chunk = True
    _tts_first_done = False

    _pcm_q = queue.Queue()
    _player_thread = None
    _player_result = {"interrupted": False}
    # Shared with play_pcm_stream so the producer can observe the player's
    # interrupt state directly -- _stop_playback alone raced: the player
    # used to clear it on exit while the producer was still blocked in
    # synthesize()/the LLM stream, so dispatch never saw the STOP.
    _player_interrupted = threading.Event()
    _tts_chars = 0  # cumulative chars dispatched to TTS this utterance

    # Fresh turn: producer owns the _stop_playback lifecycle on the
    # streaming path (play_pcm_stream no longer clears it). A STOP routed
    # while idle would otherwise falsely abort this turn's first chunk.
    _stop_playback.clear()

    # Kids gap-filler (S144): if first real audio is late, drop one short
    # playful "thinking" clip into the silence so a low-attention child stays
    # engaged. Serialized against the real player via _filler_lock so the two
    # never touch the audio device at once. Main turn only (not follow-ups).
    _filler_lock = threading.Lock()
    if (stage_prefix == "" and state.kids_mode and KIDS_GAP_FILLERS
            and _kids_filler_cache):
        def _kids_gap_filler():
            import random
            _deadline = time.monotonic() + KIDS_THINK_FILLER_MS / 1000.0
            while time.monotonic() < _deadline:
                if (_player_thread is not None or _stop_playback.is_set()
                        or _player_interrupted.is_set()):
                    return
                time.sleep(0.05)
            with _filler_lock:
                # Re-check under the lock: the real player may have started
                # while we were waiting on the lock.
                if (_player_thread is not None or _stop_playback.is_set()
                        or _player_interrupted.is_set()):
                    return
                _line = random.choice(list(_kids_filler_cache.keys()))
                _pcm_fill = _kids_filler_cache.get(_line)
                if not _pcm_fill:
                    return
                try:
                    play_pcm_speaking(_pcm_fill, pa, teensy,
                                      emotion="CURIOUS", restore_mouth_idx=0)
                    print(f"[KIDFILL] {_line!r}", flush=True)
                except Exception as _fe:
                    print(f"[KIDFILL] failed: {_fe}", flush=True)
        threading.Thread(target=_kids_gap_filler, daemon=True).start()

    def _run_player(_emotion):
        _player_result["interrupted"] = play_pcm_stream(
            _pcm_q, pa, teensy, emotion=_emotion,
            restore_mouth_idx=MOUTH_MAP.get(_emotion, 0),
            interrupted=_player_interrupted)

    try:
        for chunk, chunk_emotion in stream_ollama(
            _build_messages(), get_model(), num_predict
        ):
            # STOP checked per LLM chunk (UDP CMD, stop phrase, loud stop, button)
            if _stop_playback.is_set() or _player_interrupted.is_set():
                _interrupted = True
                print("[STOP] Stop flag set mid-stream -- halting dispatch", flush=True)
                break
            # Cumulative TTS_MAX_CHARS backstop: per-sentence synthesis means
            # _truncate_for_tts only caps each sentence, never the utterance.
            # Once the budget is spent, stop dispatching AND stop consuming
            # the LLM stream (break closes the generator -> HTTP stream).
            if _tts_chars >= TTS_MAX_CHARS:
                print(f"[TTS]  Utterance cap reached: {_tts_chars} chars dispatched >= TTS_MAX_CHARS={TTS_MAX_CHARS} -- halting stream", flush=True)
                break
            if chunk_emotion is not None and not _emotion_set:
                emit_emotion(teensy, leds, chunk_emotion)
                _current_emotion = chunk_emotion
                _emotion_set = True
            if _bench_first_chunk:
                _t_llm_first = time.time()
                _t_mono_llm_first = time.monotonic()
                try:
                    bench_stages["llm_first_token_ms"] = round((_t_mono_llm_first - _t_mono_llm0) * 1000)
                    print(f"[BENCH] t={_t_llm_first:.3f} stage={stage_prefix}llm_first_chunk dur_ttfc={_t_llm_first-_t_llm0:.2f} llm_first_token_ms={bench_stages['llm_first_token_ms']}", flush=True)
                except Exception:
                    print(f"[BENCH] t={_t_llm_first:.3f} stage={stage_prefix}llm_first_chunk dur_ttfc={_t_llm_first-_t_llm0:.2f}", flush=True)
                _bench_first_chunk = False
            reply_parts.append(chunk)

            # Synthesize this sentence. synthesize() does Kokoro->Piper
            # fallback internally; it only raises if BOTH engines fail, in
            # which case skip this sentence rather than killing the turn.
            try:
                _pcm = synthesize(chunk)
            except Exception as _se:
                print(f"[ERR]  TTS sentence skipped: {_se}", flush=True)
                continue

            # Re-check STOP after synthesize() -- it blocks ~1s+, which is
            # exactly the window the old race lived in.
            if _stop_playback.is_set() or _player_interrupted.is_set():
                _interrupted = True
                print("[STOP] Stop flag set post-synthesis -- halting dispatch", flush=True)
                break

            if not _tts_first_done:
                _t_tts = time.time()
                _t_mono_tts = time.monotonic()
                try:
                    bench_stages["tts_ms"] = round((_t_mono_tts - _t_mono_llm_first) * 1000)
                    bench_stages["engine"] = "kokoro" if KOKORO_ENABLED else "piper"
                    print(f"[BENCH] t={_t_tts:.3f} stage={stage_prefix}tts_first dur_tts={_t_tts-_t_llm_first:.2f} tts_ms={bench_stages['tts_ms']} engine={bench_stages['engine']}", flush=True)
                except Exception:
                    pass
                _tts_first_done = True

            # Start the player on the first synthesized sentence -- this is
            # where first audio begins (perceived latency = play_start_ms).
            # Acquire _filler_lock so we never open a second output stream
            # while a kids gap-filler is still playing; uncontended (instant)
            # when no filler is in flight.
            if _player_thread is None:
                with _filler_lock:
                    leds.show_speaking(); mic.stop_stream()
                    teensy.send_command("EMOTION:NEUTRAL")
                    _t_mono_play = time.monotonic()
                    try:
                        bench_stages["play_start_ms"] = round((_t_mono_play - t_mono_wake) * 1000)
                        print(f"[BENCH] stage={stage_prefix}play_start play_start_ms={bench_stages['play_start_ms']} total_ms={bench_stages['play_start_ms']}", flush=True)
                    except Exception:
                        pass
                    _player_thread = threading.Thread(
                        target=_run_player, args=(_current_emotion,), daemon=True)
                    _player_thread.start()
                    _tts_active.set()  # suppress gesture audio cues while speaking

            _pcm_q.put(_pcm)
            _tts_chars += len(chunk)
    except Exception as e:
        print(f"[ERR]  LLM stream: {e}", flush=True)
        if _player_thread is not None:
            _pcm_q.put(None)
            _player_thread.join(timeout=30)
            _interrupted = _player_result["interrupted"]
        else:
            return "", _current_emotion, False, False

    if not _emotion_set:
        emit_emotion(teensy, leds, "NEUTRAL")

    reply = " ".join(reply_parts).strip()
    print(f"[LLM]  '{reply}'", flush=True)
    _t_llm1 = time.time()
    _t_mono_llm1 = time.monotonic()
    try:
        bench_stages["llm_total_ms"] = round((_t_mono_llm1 - _t_mono_llm0) * 1000)
        print(f"[BENCH] t={_t_llm1:.3f} stage={stage_prefix}llm_done dur_llm={_t_llm1-_t_llm0:.2f} llm_total_ms={bench_stages['llm_total_ms']} reply_chars={len(reply)}", flush=True)
    except Exception:
        print(f"[BENCH] t={_t_llm1:.3f} stage={stage_prefix}llm_done dur_llm={_t_llm1-_t_llm0:.2f} reply_chars={len(reply)}", flush=True)

    # Signal end-of-stream and wait for overlapped playback to drain.
    if _player_thread is not None:
        _pcm_q.put(None)
        _player_thread.join()
        _interrupted = _player_result["interrupted"] or _interrupted
    elif not reply:
        print("[LLM]  Empty reply -- nothing to play", flush=True)

    # Turn end: clear the stop flag here, not in play_pcm_stream, so a
    # producer still mid-dispatch can never miss it.
    _stop_playback.clear()
    _tts_active.clear()

    _rpqr_state["t_last_spoke"] = time.time()
    try:
        _t_audio = time.time()
        print(f"[BENCH] t={_t_audio:.3f} stage={stage_prefix}audio_done dur_total={time.monotonic()-t_mono_wake:.2f}", flush=True)
    except Exception:
        pass
    emit_emotion(teensy, leds, _current_emotion)

    state.conversation_history.append({"role": "assistant", "content": reply})
    if len(state.conversation_history) > 20:
        state.conversation_history.pop(0); state.conversation_history.pop(0)
        if not (state.conversation_history and state.conversation_history[0].get("content", "").startswith("[Earlier conversation")):
            state.conversation_history.insert(0, {"role": "assistant", "content": "[Earlier conversation omitted]"})

    return reply, _current_emotion, _interrupted, True


# ── Kids camera games (S144) ──────────────────────────────────────────────────
# Each prompt ends by asking the child something, so the kids reply lands a hook
# and the existing follow-up loop engages -- the child can guess/respond without
# re-saying the wake word. Multi-turn guesses go to the kids text model (no image
# re-feed); the persona plays along ("ooo so close!").
_CAMERA_GAME_PROMPTS = {
    "I_SPY": ("You are playing I Spy with a young child using your camera. "
              "Look at the image, secretly pick ONE clear, obvious object you can see, "
              "and give a single playful clue like 'I spy with my little eye something...' "
              "naming only its COLOR or SHAPE -- never the object itself. "
              "One short sentence, then ask them to guess."),
    "SHOW_ME": ("A young child is holding something up to your camera. "
                "Look at the image and playfully guess what the object is in one short "
                "sentence, in character as IRIS the fun kids robot. Then ask if you got it right."),
    "FACE": ("A young child is making a face at your camera. Look at the image and "
             "playfully guess what feeling or expression they are showing, in one short "
             "sentence, in character as IRIS. Then invite them to try another face."),
}
_CAMERA_GAME_FALLBACKS = {
    "I_SPY":   "Uh oh, my camera's being shy! Want to play a riddle instead?",
    "SHOW_ME": "Hmm, my camera blinked! Hold it up again, or want a different game?",
    "FACE":    "Aw, my camera missed it! Make the face again, or want to try I Spy?",
}
_CAMERA_GAME_PROMPTS_ADULT = {
    "I_SPY": ("You are playing I Spy using your camera. "
              "Look at the image, secretly pick ONE clear, obvious object you can see, "
              "and give a single clue like 'I spy with my little eye something...' "
              "naming only its COLOR or SHAPE -- never the object itself. "
              "One short sentence, then ask them to guess."),
    "SHOW_ME": ("Someone is holding something up to your camera. "
                "Look at the image and guess what the object is in one short "
                "sentence, in character as IRIS. Then ask if you got it right."),
    "FACE": ("Someone is making a face at your camera. Look at the image and "
             "guess what feeling or expression they are showing, in one short "
             "sentence, in character as IRIS. Then invite them to try another face."),
}

# Pre-cached intro lines (S168 Break 1): spoken immediately after the wake ack
# while the blocking capture + vision inference runs, so there's no dead air.
_CAMERA_GAME_INTROS = {
    "I_SPY":   "Ooh, I Spy! Let me take a peek...",
    "SHOW_ME": "Ooh, let me see what you've got...",
    "FACE":    "Ooh, let me look at that face...",
}

# Follow-up vision prompts (S168 Break 7): for SHOW_ME / FACE the child's frame
# changes between turns, so each guess re-captures and re-asks vision with the
# guess text spliced in. I_SPY needs no re-capture (the spied object is fixed),
# so it stays on the text follow-up path.
_CAMERA_GAME_FOLLOWUP_PROMPTS = {
    "SHOW_ME": ("A young child is playing a guessing game holding something up to "
                "your camera. They just said: '{guess}'. Look at the image again and "
                "react playfully in character as IRIS the fun kids robot -- tell them "
                "if they're right, or give a fun hint and guess again. One short "
                "sentence, then invite their next guess."),
    "FACE": ("A young child is playing a face-making game. They just said: '{guess}'. "
             "Look at the image again and react playfully in character as IRIS to the "
             "face they're making now. One short sentence, then invite another face."),
}
_CAMERA_GAME_FOLLOWUP_PROMPTS_ADULT = {
    "SHOW_ME": ("Someone is playing a guessing game holding something up to your "
                "camera. They just said: '{guess}'. Look at the image again and react "
                "in character as IRIS -- tell them if they're right, or give a hint and "
                "guess again. One short sentence, then invite their next guess."),
    "FACE": ("Someone is playing a face-making game. They just said: '{guess}'. Look "
             "at the image again and react in character as IRIS to the face they're "
             "making now. One short sentence, then invite another face."),
}


def _play_camera_game(game, text, teensy, leds, pa, mic, bench_stages, t_mono_wake):
    """Kids-mode camera game turn: capture a frame, ask the kids vision model a
    game-specific prompt, speak the playful result. Returns the same
    (reply, emotion, interrupted, ok) tuple as _speak_llm_turn so the shared
    follow-up loop engages on the closing hook."""
    print(f"[GAME] Camera game: {game}", flush=True)
    state.last_interaction = time.time()
    state.conversation_history.append({"role": "user", "content": text})
    emit_emotion(teensy, leds, "CURIOUS"); leds.show_thinking()
    # Break 1 (S168): overlap the blocking capture (rpicam ~1-3s) with a short
    # pre-cached intro line so there's no dead air after the wake ack. Vision
    # inference still follows, but IRIS is no longer silent the whole time.
    _cap = {"img": None}
    def _grab(): _cap["img"] = capture_image()
    _cap_thread = threading.Thread(target=_grab, daemon=True); _cap_thread.start()
    _intro_pcm = _game_intro_cache.get(game)
    if _intro_pcm:
        try:
            leds.show_speaking(); mic.stop_stream()
            play_pcm_speaking(_intro_pcm, pa, teensy, emotion="CURIOUS", restore_mouth_idx=0)
        except Exception as _ie:
            print(f"[GAME] intro filler failed: {_ie}", flush=True)
        finally:
            try: mic.start_stream()
            except OSError: pass
            leds.show_thinking()
    _cap_thread.join(timeout=10)
    img = _cap["img"]
    emotion = "HAPPY"
    if img is None:
        reply = _CAMERA_GAME_FALLBACKS.get(game, "My camera's being shy! Want a different game?")
    else:
        _prompts = _CAMERA_GAME_PROMPTS if state.kids_mode else _CAMERA_GAME_PROMPTS_ADULT
        try:
            emotion, reply = ask_vision_game(img, _prompts[game], get_model())
            print(f"[GAME] {emotion}: '{reply}'", flush=True)
        except Exception as e:
            print(f"[ERR]  Camera game vision: {e}", flush=True)
            reply = _CAMERA_GAME_FALLBACKS.get(game, "Oops, my eyes glitched! Want a different game?")
    if not reply:
        reply = _CAMERA_GAME_FALLBACKS.get(game, "Let's try something else!")
    _interrupted = False
    try:
        pcm_data = synthesize(reply)
        emit_emotion(teensy, leds, emotion)
        leds.show_speaking(); mic.stop_stream()
        _t_mono_play = time.monotonic()
        try: bench_stages["play_start_ms"] = round((_t_mono_play - t_mono_wake) * 1000)
        except Exception: pass
        _interrupted = play_pcm_speaking(pcm_data, pa, teensy, emotion=emotion,
                                         restore_mouth_idx=MOUTH_MAP.get(emotion, 0))
        try: mic.start_stream()
        except OSError: pass
    except Exception as e:
        print(f"[ERR]  Camera game TTS: {e}", flush=True)
        return reply, emotion, False, False
    state.conversation_history.append({"role": "assistant", "content": reply})
    if len(state.conversation_history) > 20:
        state.conversation_history.pop(0); state.conversation_history.pop(0)
        if not (state.conversation_history and state.conversation_history[0].get("content", "").startswith("[Earlier conversation")):
            state.conversation_history.insert(0, {"role": "assistant", "content": "[Earlier conversation omitted]"})
    _rpqr_state["t_last_spoke"] = time.time()
    return reply, emotion, _interrupted, True


def _play_camera_game_followup(game, guess, teensy, leds, pa, mic, bench_stages, t_mono0):
    """Break 7 (S168): SHOW_ME / FACE follow-up turn. The child's frame changes
    between guesses, so re-capture and re-ask vision with the guess spliced into
    a continuation prompt. Returns the same (reply, emotion, interrupted, ok)
    tuple as _speak_llm_turn. I_SPY does NOT use this (object is fixed)."""
    print(f"[GAME] Follow-up camera game: {game} guess={guess!r}", flush=True)
    state.last_interaction = time.time()
    state.conversation_history.append({"role": "user", "content": guess})
    leds.show_thinking()
    img = capture_image()
    emotion = "HAPPY"
    if img is None:
        reply = _CAMERA_GAME_FALLBACKS.get(game, "My camera blinked! Try again?")
    else:
        _prompts = _CAMERA_GAME_FOLLOWUP_PROMPTS if state.kids_mode else _CAMERA_GAME_FOLLOWUP_PROMPTS_ADULT
        try:
            _p = _prompts[game].format(guess=guess)
            emotion, reply = ask_vision_game(img, _p, get_model())
            print(f"[GAME] {emotion}: '{reply}'", flush=True)
        except Exception as e:
            print(f"[ERR]  Camera game follow-up vision: {e}", flush=True)
            reply = _CAMERA_GAME_FALLBACKS.get(game, "Oops, my eyes glitched! Try again?")
    if not reply:
        reply = "Hmm, let's try that again!"
    _interrupted = False
    try:
        pcm_data = synthesize(reply)
        emit_emotion(teensy, leds, emotion)
        leds.show_speaking(); mic.stop_stream()
        try: bench_stages["play_start_ms"] = round((time.monotonic() - t_mono0) * 1000)
        except Exception: pass
        _interrupted = play_pcm_speaking(pcm_data, pa, teensy, emotion=emotion,
                                         restore_mouth_idx=MOUTH_MAP.get(emotion, 0))
        try: mic.start_stream()
        except OSError: pass
    except Exception as e:
        print(f"[ERR]  Camera game follow-up TTS: {e}", flush=True)
        return reply, emotion, False, False
    state.conversation_history.append({"role": "assistant", "content": reply})
    if len(state.conversation_history) > 20:
        state.conversation_history.pop(0); state.conversation_history.pop(0)
        if not (state.conversation_history and state.conversation_history[0].get("content", "").startswith("[Earlier conversation")):
            state.conversation_history.insert(0, {"role": "assistant", "content": "[Earlier conversation omitted]"})
    _rpqr_state["t_last_spoke"] = time.time()
    return reply, emotion, _interrupted, True


# ── Follow-up ─────────────────────────────────────────────────────────────────

_GAME_CONTINUE_CUES = (
    "try again", "guess again", "another", "one more", "keep going",
    "so close", "close", "nope", "not quite", "you got it", "got it",
    "your turn", "go again", "ready", "what else",
)

def implies_followup(reply: str, in_game: bool = False) -> bool:
    r = reply.strip()
    if r.endswith('?'): return True
    rl = r.lower()
    if any(rl.endswith(p) or rl.endswith(p+'.') for p in
           ("want me to", "shall i", "would you like me to", "let me know if", "go ahead")):
        return True
    # In a reciprocal camera game, exclamation-ended reactions ("Nope! Try
    # again!", "You got it!") must keep the loop alive even without a '?'.
    if in_game and any(c in rl for c in _GAME_CONTINUE_CUES):
        return True
    return False

def record_followup(mic, pa, leds, timeout=None, play_beep=True):
    if timeout is None:
        timeout = KIDS_FOLLOWUP_TIMEOUT if state.kids_mode else FOLLOWUP_TIMEOUT
    leds.show_followup()
    if play_beep: play_double_beep(pa)
    frames = []; silence = 0; speech_detected = False
    sil_secs  = KIDS_SILENCE_SECS   if state.kids_mode else SILENCE_SECS
    sil_rms   = KIDS_SILENCE_RMS    if state.kids_mode else SILENCE_RMS
    rec_secs  = KIDS_RECORD_SECONDS if state.kids_mode else RECORD_SECONDS
    sil_limit = int(SAMPLE_RATE / CHUNK * sil_secs)
    max_chunks = int(SAMPLE_RATE / CHUNK * (timeout + rec_secs))
    timeout_chunks = int(SAMPLE_RATE / CHUNK * timeout); chunks_read = 0
    mic.start_stream()
    for _ in range(max_chunks):
        f = mic.read(CHUNK, exception_on_overflow=False); chunks_read += 1
        rms = np.sqrt(np.mean(np.frombuffer(f, dtype=np.int16).astype(np.float32)**2))
        if not speech_detected:
            if rms > sil_rms: speech_detected = True; frames.append(f)
            elif chunks_read >= timeout_chunks: mic.stop_stream(); return None
        else:
            frames.append(f); silence = silence + 1 if rms < sil_rms else 0
            if silence >= sil_limit: break
    mic.stop_stream()
    return b"".join(frames) if speech_detected else None


def show_idle_for_mode(leds):
    if state.kids_mode: leds.show_idle_kids()
    else: leds.show_idle()


def in_sleep_window() -> bool:
    hour = time.localtime().tm_hour
    return hour >= SLEEP_WINDOW_START_HOUR or hour < SLEEP_WINDOW_END_HOUR


def _bench_write(stages, transcript, reply_chars, model, gandalf_was_cold, route, interrupted, emotion=None):
    """Append one structured JSON record to iris_bench.jsonl. Never raises."""
    import datetime
    try:
        os.makedirs(os.path.dirname(BENCH_LOG), exist_ok=True)
        record = {
            "ts":               datetime.datetime.now().isoformat(timespec="seconds"),
            "stages":           stages,
            "total_ms":         stages.get("play_start_ms"),
            "transcript":       transcript,
            "reply_chars":      reply_chars,
            "model":            model,
            "gandalf_was_cold": gandalf_was_cold,
            "route":            route,
            "interrupted":      interrupted,
        }
        if emotion is not None:
            record["emotion"] = emotion
        with open(BENCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
    except Exception as e:
        print(f"[BENCH] JSONL write failed: {e}", flush=True)


SLEEP_CFG_MAP = {
    "speed":           "SLEEP_ANIM_SPEED",
    "starBrightMin":   "SLEEP_ANIM_STAR_BRIGHT_MIN",
    "starBrightMax":   "SLEEP_ANIM_STAR_BRIGHT_MAX",
    "starTwinkleAmp":  "SLEEP_ANIM_STAR_TWINKLE",
    "shootCount":      "SLEEP_ANIM_SHOOT_COUNT",
    "shootSpeed":      "SLEEP_ANIM_SHOOT_SPEED",
    "shootLen":        "SLEEP_ANIM_SHOOT_LEN",
    "shootBright":     "SLEEP_ANIM_SHOOT_BRIGHT",
    "warpCount":       "SLEEP_ANIM_WARP_COUNT",
    "warpSpeed":       "SLEEP_ANIM_WARP_SPEED",
    "warpBright":      "SLEEP_ANIM_WARP_BRIGHT",
    "moonR":           "SLEEP_ANIM_MOON_R",
    "moonDrift":       "SLEEP_ANIM_MOON_DRIFT",
    "saturnR":         "SLEEP_ANIM_SATURN_R",
    "saturnDrift":     "SLEEP_ANIM_SATURN_DRIFT",
    "nebulaAlpha":     "SLEEP_ANIM_NEBULA_ALPHA",
    "waveAmp0":        "SLEEP_ANIM_WAVE_AMP0",
    "waveAmp1":        "SLEEP_ANIM_WAVE_AMP1",
    "waveAmp2":        "SLEEP_ANIM_WAVE_AMP2",
    "waveOscAmp":      "SLEEP_ANIM_WAVE_OSC_AMP",
    "mouthPulseAlpha": "SLEEP_ANIM_MOUTH_PULSE_A",
    "zzzAlpha0":       "SLEEP_ANIM_ZZZ_ALPHA0",
    "zzzAlpha1":       "SLEEP_ANIM_ZZZ_ALPHA1",
    "zzzAlpha2":       "SLEEP_ANIM_ZZZ_ALPHA2",
}


# Runtime Person Sensor config (S141). Persisted by iris_web.py to ps_config.json;
# re-asserted here on serial open because a Teensy reboot reverts to firmware
# defaults. Mirrors the SLEEP_CFG startup push.
PS_CONFIG_FILE  = "/home/pi/ps_config.json"
PS_CFG_DEFAULTS = {"CONF": 60, "FACING": 1, "LOST_MS": 5000, "Y_BIAS": 0.0, "LED": 0}


def _push_ps_config(teensy):
    """Send saved Person Sensor tuning to the Teensy. Firmware resets to compile-time
    defaults on its own reboot, so the Pi4 re-sends the operator's saved PS_CFG values
    at assistant startup. No-op safe: falls back to defaults if the file is missing."""
    try:
        try:
            with open(PS_CONFIG_FILE) as _f:
                saved = json.load(_f)
        except Exception:
            saved = {}
        cfg = {**PS_CFG_DEFAULTS,
               **{k: v for k, v in saved.items() if k in PS_CFG_DEFAULTS}}
        for key in PS_CFG_DEFAULTS:
            teensy.send_command(f"PS_CFG:{key}={cfg[key]}")
        print(f"[PSCFG] Pushed Person Sensor config: {cfg}", flush=True)
    except Exception as _e:
        print(f"[PSCFG] push failed: {_e}", flush=True)


def _mouth_intensity(kind):
    """Live MOUTH_INTENSITY_<kind> from /home/pi/iris_config.json so WebUI changes
    take effect without an assistant restart. core.config is imported once at startup
    (`from core.config import *`), so these constants would otherwise be frozen and
    WebUI 'Apply Now' would only land a one-shot push (S130). kind: AWAKE|SLEEP|IDLE."""
    default = {"AWAKE": MOUTH_INTENSITY_AWAKE,
               "SLEEP": MOUTH_INTENSITY_SLEEP,
               "IDLE":  MOUTH_INTENSITY_IDLE}[kind]
    try:
        with open("/home/pi/iris_config.json") as _f:
            v = int(json.load(_f).get(f"MOUTH_INTENSITY_{kind}", default))
        return v if 0 <= v <= 15 else default
    except Exception:
        return default


def _do_sleep(teensy, leds):
    teensy.send_command("EYES:SLEEP")
    try:
        import core.config as _cc
        try:
            with open("/home/pi/iris_config.json") as _f:
                _live = json.load(_f)
        except Exception:
            _live = {}
        for key, cfg_key in SLEEP_CFG_MAP.items():
            val = _live.get(cfg_key, getattr(_cc, cfg_key, None))
            if val is not None:
                teensy.send_command(f"SLEEP_CFG:{key}={val}")
    except Exception as _e:
        print(f"[SLEEP] SLEEP_CFG push failed: {_e}", flush=True)
    teensy.send_command("MOUTH:8")
    teensy.send_command(f"MOUTH_INTENSITY:{_mouth_intensity('SLEEP')}")
    state.eyes_sleeping = True
    open("/tmp/iris_sleep_mode", "w").close()
    leds.show_sleep()
    print("[SLEEP] _do_sleep() complete", flush=True)


def _do_wake(teensy, leds):
    teensy.send_command("EYES:WAKE")
    teensy.send_command("MOUTH:0")
    teensy.send_command(f"MOUTH_INTENSITY:{_mouth_intensity('AWAKE')}")
    state.eyes_sleeping = False
    try: os.remove("/tmp/iris_sleep_mode")
    except FileNotFoundError: pass
    show_idle_for_mode(leds)
    print("[WAKE] _do_wake() complete", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _last_known_emotion
    leds = APA102(NUM_LEDS)
    setup_button()
    from core.config import SPEAKER_VOLUME as _startup_vol
    set_volume(_startup_vol)
    print(f"[VOL]  Startup volume: {_startup_vol}/127 ({round(_startup_vol/127*100)}%)", flush=True)
    ctx_thread = threading.Thread(target=_context_watchdog, daemon=True); ctx_thread.start()
    teensy = TeensyBridge(TEENSY_PORT, TEENSY_BAUD,
                          on_reconnect=lambda: _push_ps_config(teensy))
    pa = pyaudio.PyAudio()  # created early so the CMD listener can play gesture cues
    import core.config as _bm_cfg
    if BASE_MOUNT_ENABLED:
        base_bridge = BaseMountBridge(_bm_cfg, leds)
        base_bridge.start()
    start_cmd_listener(teensy, leds, pa)
    router = IntentRouter()

    def _start_oww():
        proc = subprocess.Popen(
            ["/home/pi/wyoming-openwakeword/.venv/bin/python3", "-m", "wyoming_openwakeword",
             "--uri", f"tcp://127.0.0.1:{OWW_PORT}",
             "--custom-model-dir", "/home/pi/wyoming-openwakeword/custom", "--preload-model", WAKE_WORD,
             "--threshold", str(OWW_THRESHOLD), "--trigger-level", str(OWW_TRIGGER_LEVEL)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                socket.create_connection(("127.0.0.1", OWW_PORT), timeout=1).close()
                return proc
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        proc.kill()
        return None

    print("[INFO] Starting wyoming-openwakeword...", flush=True)
    leds.show_thinking()
    oww_proc = None
    for _attempt in range(3):
        oww_proc = _start_oww()
        if oww_proc is not None:
            break
        print(f"[ERR] openwakeword start attempt {_attempt+1}/3 failed", flush=True)
        time.sleep(2 ** _attempt)
    if oww_proc is None:
        print("[ERR] openwakeword could not start after 3 attempts -- will retry in main loop", flush=True)
        leds.show_error(); time.sleep(2)
    else:
        print("[INFO] openwakeword ready", flush=True)
    mic_idx = _find_mic_device_index()
    mic = pa.open(rate=SAMPLE_RATE, channels=CHANNELS, format=pyaudio.paInt16,
                  input=True, frames_per_buffer=CHUNK,
                  input_device_index=mic_idx)

    print(f"[INFO] Wake word  : {WAKE_WORD}", flush=True)
    print(f"[INFO] LLM adult  : {OLLAMA_MODEL_ADULT} @ {GANDALF}:{OLLAMA_PORT}", flush=True)
    print(f"[INFO] LLM kids   : {OLLAMA_MODEL_KIDS}", flush=True)
    _tts_engine = "Kokoro" if KOKORO_ENABLED else "Piper"
    print(f"[INFO] TTS        : {_tts_engine} @ {GANDALF}:8004", flush=True)
    print(f"[INFO] Teensy     : {TEENSY_PORT}", flush=True)
    print(f"[INFO] Base mount : {BASE_MOUNT_PORT} (enabled={BASE_MOUNT_ENABLED})", flush=True)
    print("[INFO] Ready.", flush=True)
    show_idle_for_mode(leds)

    # ── Power-On Self-Test ────────────────────────────────────────────────────
    try:
        from iris_post import run_post as _iris_post
        _post_result = _iris_post(leds=leds, teensy=teensy, pa=pa, verbose=True)
        if _post_result.get("verdict") == "FAIL":
            print("[POST] FAIL -- IRIS startup blocked. Check /home/pi/logs/iris_post.log",
                  flush=True)
            sys.exit(1)
    except Exception as _pe:
        print(f"[POST] POST skipped: {_pe}", flush=True)
    # Restore eye to configured default after POST display exercise
    teensy.send_command(f"EYE:{DEFAULT_EYE_IDX}")
    print(f"[EYES] Default eye restored: EYE:{DEFAULT_EYE_IDX}", flush=True)
    _push_ps_config(teensy)
    _pre_synthesize_quips()
    # Pre-warm Ollama: eliminates ~10-12s cold-start penalty on first user interaction
    if gandalf_is_up():
        try:
            requests.post(
                f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
                json={"model": get_model(), "prompt": ".", "stream": False,
                      "keep_alive": "8h",  # S134: pin model resident at boot -- kills cold-reload latency
                      "options": {"num_predict": 1}},
                timeout=30
            )
            print("[LLM]  Model warmed.", flush=True)
        except Exception as _e:
            print(f"[LLM]  Warmup skipped: {_e}", flush=True)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Sleep-state reconciliation ────────────────────────────────────────────
    # state.eyes_sleeping is in-memory and defaults False; the /tmp flag lives in
    # RAM and is cleared on full reboot. Either an assistant restart (flag may
    # survive) or a full reboot (flag gone) during the sleep window would leave
    # the Pi awake while the clock says bedtime. Reconcile from ground truth at
    # startup: if we're inside the sleep window OR the flag is set, re-assert
    # sleep authoritatively (idempotent — _do_sleep is safe to call when already
    # asleep). This hardens the scheduled sleep against restarts/reboots.
    if in_sleep_window() or os.path.exists('/tmp/iris_sleep_mode'):
        _do_sleep(teensy, leds)
        print("[SLEEP] Startup reconcile: sleep window/flag active -- sleep re-asserted", flush=True)

    try:
        while True:
            # Restart OWW process if it has died
            if oww_proc is None or oww_proc.poll() is not None:
                print("[WARN] openwakeword process not running -- attempting restart", flush=True)
                if oww_proc is not None:
                    try: oww_proc.kill()
                    except Exception: pass
                oww_proc = None
                for _attempt in range(3):
                    oww_proc = _start_oww()
                    if oww_proc is not None:
                        print("[INFO] openwakeword restarted", flush=True)
                        break
                    print(f"[ERR] openwakeword restart attempt {_attempt+1}/3 failed", flush=True)
                    time.sleep(2 ** _attempt)
                if oww_proc is None:
                    print("[ERR] openwakeword unavailable -- retrying in 10s", flush=True)
                    leds.show_error(); time.sleep(10); show_idle_for_mode(leds); continue

            try:
                oww_sock = socket.create_connection(("127.0.0.1", OWW_PORT), timeout=10)
            except (OSError, ConnectionRefusedError) as e:
                print(f"[ERR] Cannot connect to openwakeword: {e} -- retrying in 5s", flush=True)
                leds.show_error(); time.sleep(5); show_idle_for_mode(leds); continue

            try:
                trigger = wait_for_wakeword_or_button(mic, oww_sock)
            except Exception as e:
                print(f"[ERR] wait_for_wakeword_or_button exception: {e}", flush=True)
                trigger = "error"
            finally:
                try: oww_sock.close()
                except Exception: pass

            if trigger == "error":
                print("[WARN] Wakeword socket error -- reconnecting", flush=True)
                leds.show_error(); time.sleep(2); show_idle_for_mode(leds); continue

            ptt_mode = (trigger == "button")
            _t_mono_wake = 0.0
            _gandalf_was_cold = False
            _bench_stages = {
                "wake_to_record_start_ms": None, "record_duration_ms": None,
                "stt_ms": None, "router_ms": None, "llm_first_token_ms": None,
                "llm_total_ms": None, "tts_ms": None, "play_start_ms": None,
            }
            _bench_transcript = ""
            _bench_reply_chars = 0
            _bench_route = ROUTE_LLM
            _bench_interrupted = False

            if ptt_mode: print("\n[PTT]  Button pressed", flush=True); leds.show_ptt()
            else: print("\n[WAKE] Wake word detected", flush=True); leds.show_wake()

            # Sleep mode check — quip is pre-cached PCM, no Gandalf needed
            if os.path.exists('/tmp/iris_sleep_mode'):
                _do_wake(teensy, leds)
                _play_wake_quip(time.localtime().tm_hour, pa, teensy, leds)
                if in_sleep_window():
                    _do_sleep(teensy, leds)
                show_idle_for_mode(leds); continue

            # ── RPQR trigger cascade (pre-cached PCM, fires before Gandalf gate) ──
            import random as _rnd
            _now_rpqr = time.time()
            _tm_rpqr  = time.localtime()
            _h_rpqr   = _tm_rpqr.tm_hour
            _mn_rpqr  = _tm_rpqr.tm_min
            _today    = (_tm_rpqr.tm_year, _tm_rpqr.tm_mon, _tm_rpqr.tm_mday)

            _toh_cfg = _QCFG.get("top_of_hour", {})
            _fod_cfg = _QCFG.get("first_of_day", {})
            _tmg     = _QCFG.get("timing", {})
            _is_new_day = _rpqr_state["last_interaction_date"] != _today
            if _is_new_day:
                _rpqr_state["last_interaction_date"] = _today

            # Break 4 (S168): suppress the whole quip cascade while a camera game
            # is active or just ended (grace window), so a wakeword used to
            # continue the game doesn't get a snarky non-sequitur mid-game.
            _game_recent = (_cam_game_state["active"]
                            or (_cam_game_state["t_ended"] > 0
                                and _now_rpqr - _cam_game_state["t_ended"] < GAME_REENTRY_GRACE_S))
            if _game_recent:
                print("[RPQR] Suppressed (camera game active/grace)", flush=True)
            elif _is_new_day and _fod_cfg.get("enabled", True):
                _play_rpqr(_first_of_day_line(_h_rpqr),
                           _fod_cfg.get("emotion", "AMUSED"), pa, teensy, leds)
            elif (_rpqr_state["t_last_wake"] > 0
                  and _now_rpqr - _rpqr_state["t_last_wake"] < _tmg.get("double_tap_window_s", 30)
                  and _DOUBLE_TAP_QUIPS):
                _play_rpqr(_rnd.choice(_DOUBLE_TAP_QUIPS),
                           _QCFG.get("double_tap_emo", "AMUSED"), pa, teensy, leds)
            elif (_rpqr_state["t_last_spoke"] > 0
                  and _now_rpqr - _rpqr_state["t_last_spoke"] < _tmg.get("post_speech_window_s", 5)
                  and _POST_SPEECH_QUIPS):
                _play_rpqr(_rnd.choice(_POST_SPEECH_QUIPS),
                           _QCFG.get("post_speech_emo", "AMUSED"), pa, teensy, leds)
            elif (_toh_cfg.get("enabled", True)
                  and _mn_rpqr <= _tmg.get("top_of_hour_minute_window", 2)
                  and (_rpqr_state["t_last_top_of_hour"] == 0
                       or _now_rpqr - _rpqr_state["t_last_top_of_hour"] > _tmg.get("top_of_hour_cooldown_s", 600))):
                _play_rpqr(_toh_line_for(_h_rpqr),
                           _toh_cfg.get("emotion", "AMUSED"), pa, teensy, leds)
                _rpqr_state["t_last_top_of_hour"] = _now_rpqr
            else:
                _play_wake_quip(_h_rpqr, pa, teensy, leds)

            _rpqr_state["t_last_wake"] = _now_rpqr
            # ─────────────────────────────────────────────────────────────────────

            try:
                _gandalf_was_cold = not gandalf_is_up()
            except Exception:
                pass
            if not ensure_gandalf_up(leds, pa):
                leds.show_error(); time.sleep(2); show_idle_for_mode(leds); continue
            teensy.send_command(f"MOUTH_INTENSITY:{_mouth_intensity('AWAKE')}")
            _t_wake = time.time()
            _t_mono_wake = time.monotonic()
            try:
                print(f"[BENCH] t={_t_wake:.3f} stage=wake_detected trigger={'ptt' if ptt_mode else 'wake'} gandalf_was_cold={str(_gandalf_was_cold).lower()}", flush=True)
            except Exception:
                pass
            _drain_n = int(SAMPLE_RATE / CHUNK * OWW_DRAIN_SECS)
            _pre_buf = []
            for _ in range(_drain_n):
                try: _pre_buf.append(mic.read(CHUNK, exception_on_overflow=False))
                except Exception: break
            leds.show_recording(); print("[REC]  Listening...", flush=True)
            _t_mono_rec_start = time.monotonic()
            raw = b"".join(_pre_buf) + record_command(mic, ptt_mode=ptt_mode, kids_mode=state.kids_mode)
            _t_mono_rec = time.monotonic()
            arr = np.frombuffer(raw, dtype=np.int16).astype(float)
            rms = np.sqrt(np.mean(arr**2))
            print(f"[REC]  {len(raw)/2/SAMPLE_RATE:.1f}s  RMS={rms:.0f}", flush=True)
            _t_rec = time.time()
            try:
                _bench_stages["wake_to_record_start_ms"] = round((_t_mono_rec_start - _t_mono_wake) * 1000)
                _bench_stages["record_duration_ms"] = round((_t_mono_rec - _t_mono_rec_start) * 1000)
                print(f"[BENCH] t={_t_rec:.3f} stage=rec_done dur_rec={_t_rec-_t_wake:.2f} wake_to_rec_start_ms={_bench_stages['wake_to_record_start_ms']} record_duration_ms={_bench_stages['record_duration_ms']} rms={rms:.0f}", flush=True)
            except Exception:
                print(f"[BENCH] t={_t_rec:.3f} stage=rec_done dur_rec={_t_rec-_t_wake:.2f} rms={rms:.0f}", flush=True)

            # ── RMS gate + Whisper hallucination filter ────────────────────────
            if rms < SILENCE_RMS:
                print(f"[REC]  Below RMS gate ({rms:.0f} < {SILENCE_RMS}), ignoring", flush=True)
                show_idle_for_mode(leds); continue

            leds.show_thinking(); print("[STT]  Transcribing...", flush=True)
            try: text = transcribe(raw)
            except Exception as e:
                print(f"[ERR]  STT: {e}", flush=True)
                leds.show_error(); time.sleep(1); show_idle_for_mode(leds); continue

            if not text:
                print("[STT]  Empty transcript", flush=True); show_idle_for_mode(leds); continue
            print(f"[STT]  '{text}'", flush=True)
            _t_stt = time.time()
            _t_mono_stt = time.monotonic()
            _bench_transcript = text
            _snip = text[:30].replace('"', "'")
            try:
                _bench_stages["stt_ms"] = round((_t_mono_stt - _t_mono_rec) * 1000)
                print(f"[BENCH] t={_t_stt:.3f} stage=stt_done dur_stt={_t_stt-_t_rec:.2f} stt_ms={_bench_stages['stt_ms']} transcript=\"{_snip}\"", flush=True)
            except Exception:
                print(f"[BENCH] t={_t_stt:.3f} stage=stt_done dur_stt={_t_stt-_t_rec:.2f} transcript=\"{_snip}\"", flush=True)

            _text_norm = text.lower().strip().strip(".!?,;:")

            # ── STOP phrase gate (pre-router; mirrors follow-up loop) ─────────────
            # Exact match or phrase followed by space — avoids false matches on
            # "stopwatch", "quietly", "cancelled", etc.
            if any(_text_norm == phrase or _text_norm.startswith(phrase + " ")
                   for phrase in STOP_PHRASES):
                print(f"[STOP] Main-loop STOP phrase: '{text}'", flush=True)
                _stop_playback.set()
                emit_emotion(teensy, leds, "NEUTRAL")
                show_idle_for_mode(leds)
                print("[INFO] Ready.", flush=True)
                continue

            _WHISPER_HALLUCINATIONS = {
                "thank you", "thanks", "thank you very much", "thanks for watching",
                "you", "the", "bye", "bye bye", "goodbye", "see you next time",
                "please subscribe", ".", "", " ",
            }
            _WHISPER_HALLUCINATION_PATTERNS = (
                "for more information", "visit www.", "www.", ".gov", ".com",
                "subscribe to", "like and subscribe", "don't forget to",
            )
            if _text_norm in _WHISPER_HALLUCINATIONS or \
               any(_text_norm.startswith(p) or p in _text_norm for p in _WHISPER_HALLUCINATION_PATTERNS):
                print(f"[STT]  Hallucination filtered: '{text}'", flush=True)
                show_idle_for_mode(leds); continue

            # ── Clip trigger (fires before LLM; clip + LLM response) ──────────
            _clip_file = check_clip_trigger(text, emotion=_last_known_emotion)
            if _clip_file:
                _tts_active.set()
                try:
                    play_clip(_clip_file, stop_event=_stop_playback)
                except Exception as _ce:
                    print(f"[CLIP] {_ce}", flush=True)
                finally:
                    _tts_active.clear()

            # ── Intent routing ────────────────────────────────────────────────
            _result = router.classify(text, state)
            _route  = _result.route
            _bench_route = _route
            _t_mono_router = time.monotonic()
            print(f"[ROUTE] {_route}/{_result.action} conf={_result.confidence}", flush=True)
            try:
                _bench_stages["router_ms"] = round((_t_mono_router - _t_mono_stt) * 1000)
                print(f"[BENCH] stage=router_done router_ms={_bench_stages['router_ms']} route={_route}", flush=True)
            except Exception:
                pass

            # Auto-wake eyes for any route that requires interaction (not sleep/stop)
            _needs_eye_wake = (
                _route in (ROUTE_COMMAND, ROUTE_UTILITY, ROUTE_LLM)
                or (_route == ROUTE_AMBIGUOUS and _result.action not in ("SLEEP", "STOP"))
            )
            if state.eyes_sleeping and _needs_eye_wake:
                state.eyes_sleeping = False
                teensy.send_command("EYES:WAKE")
                teensy.send_command(f"MOUTH_INTENSITY:{_mouth_intensity('AWAKE')}")
                print("[EYES] Eyes auto-waked by interaction", flush=True)

            if _route == ROUTE_REFLEX:
                if _result.action == "SLEEP":
                    _do_sleep(teensy, leds)
                    if _result.response:
                        try:
                            pcm_data = synthesize(_result.response)
                            leds.show_speaking(); mic.stop_stream()
                            _t_mono_play = time.monotonic()
                            try: _bench_stages["play_start_ms"] = round((_t_mono_play - _t_mono_wake) * 1000)
                            except Exception: pass
                            play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
                            _bench_write(_bench_stages, _bench_transcript, 0, get_model(), _gandalf_was_cold, ROUTE_REFLEX, False)
                        except Exception as e:
                            print(f"[ERR]  TTS reflex sleep: {e}", flush=True)
                    show_idle_for_mode(leds); print("[INFO] Ready.", flush=True); continue
                elif _result.action == "STOP":
                    print("[STOP] Stop command received", flush=True)
                    _stop_playback.set(); emit_emotion(teensy, leds, "NEUTRAL")
                    show_idle_for_mode(leds); continue
                elif _result.action == "WAKE":
                    _do_wake(teensy, leds)
                    show_idle_for_mode(leds); print("[INFO] Ready.", flush=True); continue

            elif _route == ROUTE_COMMAND:
                if _result.action == "EYES_SLEEP":
                    # Route through the authoritative _do_sleep() so a voice sleep
                    # is identical to the scheduled/quip path: SLEEP_CFG burst, sleep
                    # face (MOUTH:8), sleep LEDs, /tmp flag written, state set.
                    # Unconditional/idempotent — never a silent no-op on state desync.
                    _do_sleep(teensy, leds)
                    print("[EYES] Eyes deactivated by voice", flush=True)
                    continue
                elif _result.action == "EYES_WAKE":
                    _do_wake(teensy, leds)
                    print("[EYES] Eyes activated by voice", flush=True)
                    continue
                elif _result.action in ("KIDS_ON", "KIDS_OFF"):
                    kids_reply, new_mode = handle_kids_mode_command(text)
                    if kids_reply is not None:
                        print(f"[MODE] {kids_reply}", flush=True)
                        leds.show_kids_mode_on() if new_mode else leds.show_kids_mode_off()
                        time.sleep(0.6)
                        try:
                            pcm_data = synthesize(kids_reply)
                            leds.show_speaking(); mic.stop_stream()
                            _t_mono_play = time.monotonic()
                            try: _bench_stages["play_start_ms"] = round((_t_mono_play - _t_mono_wake) * 1000)
                            except Exception: pass
                            play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
                            _bench_write(_bench_stages, _bench_transcript, 0, get_model(), _gandalf_was_cold, ROUTE_COMMAND, False)
                        except Exception as e:
                            print(f"[ERR]  TTS mode switch: {e}", flush=True)
                    emit_emotion(teensy, leds, "NEUTRAL"); show_idle_for_mode(leds)
                    print("[INFO] Ready.", flush=True); continue
                else:
                    # Volume commands
                    vol_reply = handle_volume_command(text)
                    if vol_reply is not None:
                        print(f"[VOL]  {vol_reply}", flush=True)
                        try:
                            pcm_data = synthesize(vol_reply)
                            leds.show_speaking(); mic.stop_stream()
                            _t_mono_play = time.monotonic()
                            try: _bench_stages["play_start_ms"] = round((_t_mono_play - _t_mono_wake) * 1000)
                            except Exception: pass
                            play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
                            _bench_write(_bench_stages, _bench_transcript, 0, get_model(), _gandalf_was_cold, ROUTE_COMMAND, False)
                        except Exception as e:
                            print(f"[ERR]  TTS vol: {e}", flush=True)
                        emit_emotion(teensy, leds, "NEUTRAL"); show_idle_for_mode(leds)
                        print("[INFO] Ready.", flush=True); continue

            elif _route == ROUTE_UTILITY:
                if _result.action == "VISION":
                    if CAMERA_ENABLED:
                        print("[CAM]  Vision trigger detected", flush=True)
                        emit_emotion(teensy, leds, "CURIOUS"); leds.show_thinking()
                        img = capture_image()
                        if img is None:
                            reply = "Sorry, I could not capture an image right now."
                        else:
                            print(f"[CAM]  Captured {len(img)//1024}KB", flush=True)
                            try:
                                reply = ask_vision(img, text)
                                print(f"[VIS]  '{reply}'", flush=True)
                            except Exception as e:
                                reply = "I had trouble processing the image."
                                print(f"[ERR]  Vision: {e}", flush=True)
                        try:
                            pcm_data = synthesize(reply)
                            leds.show_speaking(); mic.stop_stream()
                            _t_mono_play = time.monotonic()
                            try: _bench_stages["play_start_ms"] = round((_t_mono_play - _t_mono_wake) * 1000)
                            except Exception: pass
                            play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
                            _bench_write(_bench_stages, _bench_transcript, len(reply), get_model(), _gandalf_was_cold, ROUTE_UTILITY, False)
                        except Exception as e:
                            print(f"[ERR]  TTS vision: {e}", flush=True)
                        emit_emotion(teensy, leds, "NEUTRAL"); show_idle_for_mode(leds)
                        print("[INFO] Ready.", flush=True); continue
                elif _result.response is not None:
                    print(f"[UTIL] {_result.action}: {_result.response}", flush=True)
                    try:
                        pcm_data = synthesize(_result.response)
                        leds.show_speaking(); mic.stop_stream()
                        _t_mono_play = time.monotonic()
                        try: _bench_stages["play_start_ms"] = round((_t_mono_play - _t_mono_wake) * 1000)
                        except Exception: pass
                        play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
                        _bench_write(_bench_stages, _bench_transcript, len(_result.response), get_model(), _gandalf_was_cold, ROUTE_UTILITY, False)
                    except Exception as e:
                        print(f"[ERR]  TTS utility: {e}", flush=True)
                    emit_emotion(teensy, leds, "NEUTRAL"); show_idle_for_mode(leds)
                    print("[INFO] Ready.", flush=True); continue

            elif _route == ROUTE_AMBIGUOUS:
                if _result.action == "STOP":
                    print("[STOP] Ambiguous stop command received", flush=True)
                    _stop_playback.set(); emit_emotion(teensy, leds, "NEUTRAL")
                    show_idle_for_mode(leds); continue
                elif _result.action == "SLEEP":
                    _do_sleep(teensy, leds)
                    if _result.response:
                        try:
                            pcm_data = synthesize(_result.response)
                            leds.show_speaking(); mic.stop_stream()
                            _t_mono_play = time.monotonic()
                            try: _bench_stages["play_start_ms"] = round((_t_mono_play - _t_mono_wake) * 1000)
                            except Exception: pass
                            play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
                            _bench_write(_bench_stages, _bench_transcript, 0, get_model(), _gandalf_was_cold, ROUTE_AMBIGUOUS, False)
                        except Exception as e:
                            print(f"[ERR]  TTS ambiguous sleep: {e}", flush=True)
                    show_idle_for_mode(leds); print("[INFO] Ready.", flush=True); continue
                # AMBIGUOUS/LLM falls through to LLM below

            # ── Streaming LLM → per-sentence TTS → overlapped playback ─────────
            # _speak_llm_turn() owns the whole turn: stream_ollama → per-sentence
            # synthesize → background player, emotion on first chunk, STOP per
            # sentence, history append + trim. See the helper docstring.
            # Kids camera games (S144) intercept before the LLM turn. The game
            # returns the same tuple as _speak_llm_turn so the follow-up loop
            # below runs unchanged.
            _cam_game = classify_camera_game(text)
            if _cam_game and CAMERA_ENABLED:
                reply, _current_emotion, _interrupted, _ok = _play_camera_game(
                    _cam_game, text, teensy, leds, pa, mic, _bench_stages, _t_mono_wake)
                if not _ok:
                    leds.show_error(); time.sleep(1)
                    show_idle_for_mode(leds); continue
                _bench_write(_bench_stages, _bench_transcript, len(reply), get_model(), _gandalf_was_cold, ROUTE_UTILITY, _interrupted, emotion=_current_emotion)
                _last_known_emotion = _current_emotion
                # Break 3 (S168): mark the game active so the follow-up loop keeps
                # offering reciprocal turns without the child re-saying the wake word.
                _cam_game_state.update(active=True, game=_cam_game,
                                       turns_remaining=GAME_FOLLOWUP_TURNS, t_ended=0.0)
            else:
                _num_predict = classify_response_length(text)
                reply, _current_emotion, _interrupted, _ok = _speak_llm_turn(
                    text, _num_predict, teensy, leds, pa, mic,
                    _bench_stages, _t_mono_wake, gandalf_was_cold=_gandalf_was_cold)
                if not _ok:
                    leds.show_error(); time.sleep(1)
                    show_idle_for_mode(leds); continue
                _bench_write(_bench_stages, _bench_transcript, len(reply), get_model(), _gandalf_was_cold, _bench_route, _interrupted, emotion=_current_emotion)
                _last_known_emotion = _current_emotion
            _bench_interrupted = _interrupted
            # Camera-game clues (e.g. "...something round!") don't always end on a
            # "?", so force at least one follow-up turn so the child can guess
            # without re-saying the wake word; cleared after the first turn.
            _force_followup = bool(_cam_game)

            if button_pressed(): time.sleep(0.4)

            # ── Follow-up loop ─────────────────────────────────────────────────
            # Break 3 (S168): in a camera game keep the loop alive for up to
            # GAME_FOLLOWUP_TURNS reciprocal turns (decrementing turns_remaining),
            # independent of implies_followup, so the game survives "Nope! Try
            # again!" style replies that don't end on '?'.
            _followup_turns = 0
            while ((implies_followup(reply, in_game=_cam_game_state["active"])
                    or _force_followup
                    or (_cam_game_state["active"] and _cam_game_state["turns_remaining"] > 0))
                   and _followup_turns < (GAME_FOLLOWUP_TURNS if _cam_game_state["active"] else FOLLOWUP_MAX_TURNS)
                   and not _interrupted):
                _max_fu = GAME_FOLLOWUP_TURNS if _cam_game_state["active"] else FOLLOWUP_MAX_TURNS
                print(f"[FLWP] Follow-up turn {_followup_turns+1}/{_max_fu}...", flush=True)
                _followup_turns += 1
                _force_followup = False
                if _cam_game_state["active"] and _cam_game_state["turns_remaining"] > 0:
                    _cam_game_state["turns_remaining"] -= 1
                # Break 5 (S168): no double-beep between game exchanges (the clue
                # already invites the guess); keep it for normal follow-ups.
                followup_audio = record_followup(mic, pa, leds,
                                                 play_beep=not _cam_game_state["active"])
                if followup_audio is None: print("[FLWP] No response", flush=True); break
                rms = np.sqrt(np.mean(np.frombuffer(followup_audio, dtype=np.int16).astype(np.float32)**2))
                if rms < 100: print("[FLWP] Silent", flush=True); break
                # Break 8 (S168): purple "your turn" LED while transcribing the
                # guess; blue "thinking" LED is set only for LLM inference below.
                leds.show_followup(); print("[STT]  Transcribing follow-up...", flush=True)
                _t_mono_fu0 = time.monotonic()
                try: text = transcribe(followup_audio)
                except Exception as e: print(f"[ERR]  STT follow-up: {e}", flush=True); break
                # Per-follow-up-turn bench stages (caller-owned, like _bench_stages)
                _fu_stages = {"stt_ms": round((time.monotonic() - _t_mono_fu0) * 1000)}
                if not text: print("[FLWP] Empty transcript", flush=True); break
                print(f"[STT]  '{text}'", flush=True)
                _text_norm = text.lower().strip().strip(".!?,;:")
                # Gate: known Whisper hallucinations (brief phrases Whisper hallucinates when silent)
                if _text_norm in _WHISPER_HALLUCINATIONS:
                    print(f"[FLWP] Hallucination filtered: '{text}'", flush=True); break
                # Gate: URL/spam hallucination patterns
                if any(p in _text_norm for p in ("www.", ".gov", ".com", ".org",
                       "for more information", "subscribe", "don't forget")):
                    print(f"[FLWP] Hallucination filtered: '{text}'", flush=True); break
                if any(_text_norm == phrase or _text_norm.startswith(phrase)
                       for phrase in STOP_PHRASES):
                    print("[STOP] Stop in follow-up", flush=True); break
                if any(_text_norm == phrase or _text_norm.startswith(phrase)
                       for phrase in FOLLOWUP_DISMISSALS):
                    print("[FLWP] Polite dismissal, ending follow-up", flush=True); break
                time_reply = handle_time_command(text)
                vol_reply  = handle_volume_command(text) if time_reply is None else None
                if time_reply is not None or vol_reply is not None:
                    # Local fast-path: time/volume replies skip the LLM and play
                    # as one pre-synthesized blob (no streaming needed).
                    reply = time_reply if time_reply is not None else vol_reply
                    emotion = "NEUTRAL"
                    emit_emotion(teensy, leds, emotion)
                    print("[TTS]  Synthesizing...", flush=True)
                    try: pcm_data = synthesize(reply)
                    except Exception as e: print(f"[ERR]  TTS follow-up: {e}", flush=True); break
                    leds.show_speaking(); mic.stop_stream()
                    _interrupted = play_pcm_speaking(pcm_data, pa, teensy, emotion=emotion,
                                                     restore_mouth_idx=MOUTH_MAP.get(emotion, 0))
                    _rpqr_state["t_last_spoke"] = time.time()
                elif _cam_game_state["active"] and _cam_game_state["game"] in ("SHOW_ME", "FACE"):
                    # Break 7 (S168): SHOW_ME / FACE follow-ups re-capture the
                    # child's changed frame and re-ask vision, instead of
                    # answering the guess from stale text context only.
                    reply, emotion, _interrupted, _fu_ok = _play_camera_game_followup(
                        _cam_game_state["game"], text, teensy, leds, pa, mic,
                        _fu_stages, _t_mono_fu0)
                    if not _fu_ok:
                        break
                    _bench_write(_fu_stages, text, len(reply), get_model(),
                                 False, "GAME_FU", _interrupted, emotion=emotion)
                    _last_known_emotion = emotion
                else:
                    # Streaming LLM follow-up: same pipeline as the main turn,
                    # so first audio starts on the first sentence instead of
                    # blocking for full generation + full synthesis (S126).
                    leds.show_thinking()
                    # Break 6 (S168): in-game guesses (I_SPY) get terse reactions
                    # -- don't over-allocate tokens for "Nope! Try again!".
                    _followup_predict = (NUM_PREDICT_SHORT if _cam_game_state["active"]
                                         else classify_response_length(text))
                    reply, emotion, _interrupted, _fu_ok = _speak_llm_turn(
                        text, _followup_predict, teensy, leds, pa, mic,
                        _fu_stages, _t_mono_fu0, stage_prefix="fu_")
                    if not _fu_ok:
                        break
                    _bench_write(_fu_stages, text, len(reply), get_model(),
                                 False, "FOLLOWUP", _interrupted, emotion=emotion)
                    _last_known_emotion = emotion
                if button_pressed(): time.sleep(0.4)
                if _interrupted:
                    print("[STOP] Playback interrupted mid-follow-up", flush=True); break

            # Break 3/4 (S168): game over -- clear the active flag and stamp the
            # end time so the RPQR quip cascade stays muted for the grace window.
            if _cam_game_state["active"]:
                _cam_game_state["active"] = False
                _cam_game_state["t_ended"] = time.time()
                print("[GAME] Game ended -- RPQR grace window started", flush=True)

            try:
                mic.start_stream()
            except OSError:
                pass
            # Discard mic audio captured during/after TTS playback to prevent
            # speaker echo from triggering a false wake on the next listen cycle.
            _post_drain_n = int(SAMPLE_RATE / CHUNK * OWW_POST_PLAY_DRAIN_SECS)
            for _ in range(_post_drain_n):
                try:
                    mic.read(CHUNK, exception_on_overflow=False)
                except Exception:
                    break
            emit_emotion(teensy, leds, "NEUTRAL")
            teensy.send_command(f"MOUTH_INTENSITY:{_mouth_intensity('IDLE')}")
            show_idle_for_mode(leds)
            if in_sleep_window():
                _do_sleep(teensy, leds)
                print("[SLEEP] Returned to sleep (sleep window active)", flush=True)
            print("[INFO] Ready.", flush=True)

    except KeyboardInterrupt:
        print("\n[INFO] Shutting down.", flush=True)
    finally:
        flush_conversation_log(reason="shutdown")
        emit_emotion(teensy, leds, "NEUTRAL"); teensy.close()
        leds.close(); gpio_cleanup()
        mic.stop_stream(); mic.close(); pa.terminate()
        if oww_proc is not None:
            oww_proc.terminate()


if __name__ == "__main__":
    main()
