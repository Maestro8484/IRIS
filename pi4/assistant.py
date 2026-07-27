#!/usr/bin/env python3
"""
assistant.py - Pi4 IRIS voice assistant
Wake: wyoming-openwakeword hey_iris (:10400) OR button press (GPIO17)
STT:  Wyoming Whisper  @ 192.168.0.20:10300
LLM:  Ollama           @ 192.168.0.20:11434 (streaming)
TTS:  Kokoro           @ 192.168.0.20:8004 (primary) / Piper @ :10200 (fallback)
Audio: wm8960-soundcard (dynamic card detection)
LEDs: 3x APA102 via SPI -- status indicator
Eyes: Teensy 4.1 via /dev/ttyIRIS_EYES
Base: Teensy 4.0 servo/gesture via /dev/ttyIRIS_SERVO (BaseMountBridge)
"""

# S199: `random` added at module level -- _speak_kids_reask (S197c) called
# random.choice with only function-local imports elsewhere in the file, a
# latent NameError on the first live kids re-ask (it had fired 0 times).
import json, os, queue, random, re, socket, subprocess, sys, threading, time
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
    play_endpoint_cue, play_your_turn_cue,
    record_command, _stop_playback, STOP_PHRASES, FOLLOWUP_DISMISSALS,
)
from services.wyoming import wy_send, read_line
from services.stt import transcribe
from services.tts import synthesize, synthesize_captioned
from services.llm import stream_ollama, classify_response_length, is_story_continuation
from services.vision import (
    capture_image, is_vision_trigger, ask_vision, ask_vision_game, classify_camera_game,
    ask_ispy_pick, ask_rps_hand,
)
from core.camera_games import (
    build_ispy_clues, judge_ispy_guess, is_affirmative, is_negative, is_giveup,
    ispy_start_line, ispy_wrong_line, ispy_correct_line, ispy_reveal_line,
    game_signoff_line, RPS_MOVES, rps_pick_move, rps_winner,
    rps_countdown_line, rps_unclear_line, rps_skip_line, rps_result_line,
    RPS_THROW_BEATS,
)
from services.wakeword import wait_for_wakeword_or_button
from state.state_manager import state
from core.intent_router import (
    IntentRouter, IntentResult,
    ROUTE_REFLEX, ROUTE_COMMAND, ROUTE_UTILITY, ROUTE_AMBIGUOUS, ROUTE_LLM,
)
from core.clip_triggers import check_clip_trigger
from core.clip_player import play_clip
from core.error_voice import speak_error
from core.speech_gates import (
    phrase_matches, is_whisper_hallucination, implies_followup,
    user_invites_followup,
    WHISPER_HALLUCINATIONS, WHISPER_HALLUCINATION_PATTERNS,
)


def get_model() -> str:
    return OLLAMA_MODEL_KIDS if state.kids_mode else OLLAMA_MODEL_ADULT


# RD-047: mirror kids_mode to a flag file so iris_web.py can report the LIVE mode
# in the WebUI Kids card. Same pattern as /tmp/iris_sleep_mode. Without this the
# toggle would be write-only and would lie whenever the inactivity watchdog or a
# voice command flipped the mode behind the UI's back.
_KIDS_MODE_FLAG = "/tmp/iris_kids_mode"


def _sync_kids_mode_flag() -> None:
    """Make the flag file match state.kids_mode. Never raises."""
    try:
        if state.kids_mode:
            open(_KIDS_MODE_FLAG, "w").close()
        elif os.path.exists(_KIDS_MODE_FLAG):
            os.remove(_KIDS_MODE_FLAG)
    except Exception as _e:
        print(f"[MODE] kids flag sync failed: {_e}", flush=True)


def _set_kids_mode(on: bool) -> None:
    """Single writer for state.kids_mode -- keeps the flag file in step."""
    state.kids_mode = bool(on)
    if on:
        # F1 (MAD): start the inactivity clock fresh whenever kids mode is ENABLED.
        # The WebUI KIDS:ON path does not run through the main turn loop that stamps
        # last_interaction, so without this the _context_watchdog could see a stale
        # timestamp from a turn >KIDS_MODE_INACTIVITY_TIMEOUT ago and auto-off kids
        # mode within 30s of the operator enabling it -- the toggle disabling itself.
        state.last_interaction = time.time()
    _sync_kids_mode_flag()


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
    sw_obj = q.get("sleep_window") or {}
    qcfg = {
        "top_of_hour":     q.get("top_of_hour") or {},
        "first_of_day":    q.get("first_of_day") or {},
        "double_tap_emo":  dbl_obj.get("emotion", "AMUSED"),
        "post_speech_emo": post_obj.get("emotion", "AMUSED"),
        "timing":          q.get("rpqr_timing") or {},
        # RD-047: honest in-window lines that teach the double-tap instead of
        # inviting a reply the sleep branch is about to discard.
        "sleep_window":     _lines("sleep_window"),
        "sleep_window_emo": sw_obj.get("emotion", "SLEEPY"),
        # RD-047 Part 3: second-stage think filler + the never-be-silent re-ask.
        "kids_fillers_stage2": _lines("kids_fillers_stage2"),
        "kids_reask":          _lines("kids_reask"),
        "kids_reask_emo":      (q.get("kids_reask") or {}).get("emotion", "CURIOUS"),
        # S199 T2/T4: kid-register wake bank + spoken kids-mode-off sign-off.
        "kids_wake":         _lines("kids_wake"),
        "kids_wake_emo":     (q.get("kids_wake") or {}).get("emotion", "CURIOUS"),
        "kids_mode_off":     _lines("kids_mode_off"),
        "kids_mode_off_emo": (q.get("kids_mode_off") or {}).get("emotion", "SLEEPY"),
        # S202: quiet-break spoken banks (entry ack / resume quips / resume Q).
        # Share the _rpqr_cache + _play_rpqr player, same as sleep_window.
        "break_ack":            _lines("break_ack"),
        "break_ack_emo":        (q.get("break_ack") or {}).get("emotion", "SLEEPY"),
        "break_resume":         _lines("break_resume"),
        "break_resume_emo":     (q.get("break_resume") or {}).get("emotion", "AMUSED"),
        "break_resume_ask":     _lines("break_resume_ask"),
        "break_resume_ask_emo": (q.get("break_resume_ask") or {}).get("emotion", "CURIOUS"),
    }
    return wake, dbl, post, kids, gesture, qcfg


(_WAKE_QUIPS, _DOUBLE_TAP_QUIPS, _POST_SPEECH_QUIPS,
 _KIDS_THINK_FILLERS, _GESTURE_CUES, _QCFG) = _build_quip_structs()
_kids_filler_cache: dict = {}
_kids_filler2_cache: dict = {}   # RD-047: second-stage (~5s) think fillers
_kids_reask_cache: dict = {}     # RD-047: spoken re-ask on the silent-drop paths
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
# S218: the four RPS throw beats as cached PCM, keyed by the synthesis speed
# they were rendered at -- GAME_COUNTDOWN_SPEED is live-tunable from the WebUI,
# and a cache keyed only by text would keep serving the old tempo after a save
# with no way to tell. A speed the cache has not seen synthesises once (four
# short words, measured ~0.18 s each) and is then free for every later round.
_rps_beat_cache: dict = {}
_last_quip_line: str = ""

# mutable state dict — avoids global declarations in main loop
_rpqr_state: dict = {
    "t_last_wake":          0.0,
    "t_last_spoke":         0.0,
    "last_interaction_date": None,
    "t_last_top_of_hour":   0.0,
}

# S194: sleep-window double-wake break-through. A lone wakeword during the sleep
# window plays a quip and re-sleeps; a SECOND wakeword within
# SLEEP_DOUBLE_WAKE_WINDOW_S falls through to a full listen-and-respond turn.
# `t_last` stamps the previous in-window wake (0.0 = none pending / just consumed).
_sleep_wake_state: dict = {"t_last": 0.0}

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
    # S210: per-game round data. I_SPY: secret/synonyms/clues/clue_idx/
    # awaiting_replay (the stored pick is what makes guesses code-judgeable).
    # RPS: kid/iris scores. Cleared at each game start.
    "data":            {},
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


def _play_kids_wake_quip(pa, teensy, leds) -> bool:
    """S199 T2: kid-register wake ack. Kids mode was drawing the ADULT snark
    bands ("Still here. Unfortunately.") -- there was no kids wake bank at all.
    Shares the _rpqr_cache/_play_rpqr player (sleep_window pattern). Returns
    False if nothing is cached yet so the caller can fall back, never silent."""
    global _last_quip_line
    cached = [l for l in (_QCFG.get("kids_wake") or []) if _rpqr_cache.get(l)]
    if not cached:
        return False
    choices = [l for l in cached if l != _last_quip_line] or cached
    line = random.choice(choices)
    _last_quip_line = line
    _play_rpqr(line, _QCFG.get("kids_wake_emo", "CURIOUS"), pa, teensy, leds)
    return True


def _play_wake_quip(hour: int, pa, teensy, leds) -> None:
    # S199 T2: kids mode draws from the kid-register bank at every call site
    # (cascade default, sleep-window fallback, manual-sleep wake). Falls through
    # to the adult bands only when no kids_wake line is cached yet.
    if state.kids_mode and _play_kids_wake_quip(pa, teensy, leds):
        return
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


def _speak_kids_reask(pa, teensy, leds) -> bool:
    """RD-047 (1C). Never end a kid's turn in silence.

    The three drop paths -- below the RMS gate, empty transcript, Whisper
    hallucination -- all just `continue`d. An adult reads that as "she didn't
    catch it"; a 6-year-old reads it as being ignored, which is worse than a
    wrong answer. Speak a cached, in-character re-ask instead. Kids mode only.
    Returns True if a line was actually played.
    """
    # Choose ONLY from lines that are actually cached (F3 / MAD): picking a random
    # configured line and *then* checking the cache could return silent when a
    # sibling line was available -- which defeats the entire never-silent point of
    # this function. A re-ask fires on the STT-failure path, exactly where synth is
    # unavailable, so an uncached line must never be selected.
    cached = [l for l in (_QCFG.get("kids_reask") or []) if _kids_reask_cache.get(l)]
    if not cached:
        print("[REASK] no cached re-ask line -- staying silent", flush=True)
        return False
    line = random.choice(cached)
    pcm = _kids_reask_cache[line]
    try:
        emit_emotion(teensy, leds, _QCFG.get("kids_reask_emo", "CURIOUS"))
        play_pcm_speaking(pcm, pa, teensy, restore_mouth_idx=0)
        print(f"[REASK] {line!r}", flush=True)
        return True
    except Exception as _e:
        print(f"[REASK] Failed: {_e}", flush=True)
        return False


def _speak_kids_mode_off(pa, teensy, leds) -> bool:
    """S199 T4 (contract rung 8). Kids-mode auto-off was SILENT: hours later a
    kid's "hey Iris" landed on the ADULT persona with no warning (observed live
    2026-07-10 20:14, the "wind-up doll" reply). Speak a cached sign-off that
    also teaches the way back in ("play with me"). Cached-only, never synths."""
    import core.config as _cfg_live
    if not getattr(_cfg_live, "KIDS_MODE_OFF_SPOKEN", 1):
        return False
    cached = [l for l in (_QCFG.get("kids_mode_off") or []) if _rpqr_cache.get(l)]
    if not cached:
        print("[MODE] no cached kids-off line -- silent revert", flush=True)
        return False
    line = random.choice(cached)
    try:
        emit_emotion(teensy, leds, _QCFG.get("kids_mode_off_emo", "SLEEPY"))
        play_pcm_speaking(_rpqr_cache[line], pa, teensy, restore_mouth_idx=0)
        print(f"[MODE] kids-off sign-off: {line!r}", flush=True)
        return True
    except Exception as _e:
        print(f"[MODE] kids-off sign-off failed: {_e}", flush=True)
        return False


def _play_sleep_window_quip(pa, teensy, leds) -> bool:
    """RD-047. Speak an honest in-window sleep line that teaches the double-tap.
    Returns False if the category is empty/disabled or nothing is cached, so the
    caller can fall back to the ordinary wake quip rather than going silent."""
    # Choose only from cached lines (F3 / MAD): pick-then-check could return False
    # and drop to the fallback wake quip even when a cached sleep line existed.
    cached = [l for l in (_QCFG.get("sleep_window") or []) if _rpqr_cache.get(l)]
    if not cached:
        print("[SLEEPQ] no cached sleep line -- falling back to wake quip", flush=True)
        return False
    line = random.choice(cached)
    _play_rpqr(line, _QCFG.get("sleep_window_emo", "SLEEPY"), pa, teensy, leds)
    return True


# ── S202: Quiet break ("do not disturb") ──────────────────────────────────────

def _play_break_cat(cat_key: str, emo_key: str, pa, teensy, leds) -> bool:
    """Play a random CACHED line from a break soundboard category via the shared
    RPQR player. Cached-only: the resume path runs with GandalfAI asleep, so an
    uncached line must never be chosen. Returns False (never raises) so the break
    flow proceeds regardless of whether a line was available."""
    cached = [l for l in (_QCFG.get(cat_key) or []) if _rpqr_cache.get(l)]
    if not cached:
        print(f"[BREAK] no cached line for {cat_key} -- skipping", flush=True)
        return False
    line = random.choice(cached)
    _play_rpqr(line, _QCFG.get(emo_key, "NEUTRAL"), pa, teensy, leds)
    return True


def _recover_mic(mic, pa, _opener=None):
    """S240f: bring a stopped or closed mic stream back, returning a usable handle.

    The wakeword loop's error branch used to reconnect only the OpenWakeWord
    socket, so once the mic stream went bad every retry raised the same error
    forever and IRIS was permanently deaf until a service restart. Observed live
    2026-07-25 after a quiet break: hundreds of "[Errno -9988] Stream closed" in a
    tight loop (the red confirm LED kept animating because the anim thread
    survives; it is the main loop that is stuck).

    Three cases, cheapest first: an ACTIVE stream is left alone; a merely STOPPED
    stream is restarted in place; a CLOSED stream (is_active() itself raises) is
    rebuilt from scratch. Every failure path returns the original handle rather
    than raising, so the caller's retry loop can only ever improve on spinning.
    `_opener` is injected by the selftest; production passes None and gets the
    real pa.open().
    """
    try:
        if mic.is_active():
            return mic
        mic.start_stream()
        print("[WARN] mic stream restarted", flush=True)
        return mic
    except Exception as _me:
        print(f"[WARN] mic restart failed ({_me}) -- reopening", flush=True)
    try:
        try: mic.close()
        except Exception: pass
        if _opener is None:
            def _opener():
                return pa.open(rate=SAMPLE_RATE, channels=CHANNELS,
                               format=pyaudio.paInt16, input=True,
                               frames_per_buffer=CHUNK,
                               input_device_index=_find_mic_device_index())
        fresh = _opener()
        print("[WARN] mic stream reopened", flush=True)
        return fresh
    except Exception as _re:
        print(f"[ERR]  mic reopen failed ({_re}) -- retrying", flush=True)
        return mic


def _recover_mic_selftest() -> int:
    """Offline, no audio hardware. assistant.py imports RPi.GPIO at module level,
    so this is run by AST-extracting the two functions rather than importing:
        python3 -c "import ast;s=open('assistant.py').read();t=ast.parse(s);
        m=ast.Module(body=[n for n in t.body if getattr(n,'name','') in
        {'_recover_mic','_recover_mic_selftest'}],type_ignores=[]);
        ns={};exec(compile(m,'<x>','exec'),ns);ns['_recover_mic_selftest']()"
    Works identically on SuperMaster and against the deployed Pi bytes."""
    results = []

    class _Mic:
        def __init__(self, state): self.state = state; self.started = False; self.closed = False
        def is_active(self):
            if self.state == "closed": raise OSError("[Errno -9988] Stream closed")
            return self.state == "active"
        def start_stream(self):
            if self.state == "closed": raise OSError("[Errno -9988] Stream closed")
            self.started = True; self.state = "active"
        def close(self): self.closed = True

    # 1. Active stream: untouched, same handle back.
    m = _Mic("active")
    out = _recover_mic(m, None)
    results.append(("active stream left alone", out is m and not m.started and not m.closed))

    # 2. Stopped stream: restarted in place, same handle back.
    m = _Mic("stopped")
    out = _recover_mic(m, None)
    results.append(("stopped stream restarted", out is m and m.started and not m.closed))

    # 3. Closed stream: old one closed, a NEW handle returned.
    m = _Mic("closed")
    fresh = _Mic("active")
    out = _recover_mic(m, None, _opener=lambda: fresh)
    results.append(("closed stream reopened", out is fresh and m.closed))

    # 4. Reopen itself fails: must NOT raise, must return the original handle.
    m = _Mic("closed")
    def _boom(): raise OSError("device busy")
    out = _recover_mic(m, None, _opener=_boom)
    results.append(("reopen failure returns original, no raise", out is m))

    # 5. The live symptom: a closed stream must never come back still closed.
    m = _Mic("closed")
    fresh = _Mic("active")
    out = _recover_mic(m, None, _opener=lambda: fresh)
    results.append(("recovered handle is usable", out.is_active() is True))

    ok = sum(1 for _, p in results if p)
    for name, passed in results:
        if not passed:
            print(f"FAIL {name}")
    print(f"_recover_mic selftest: {ok}/{len(results)} PASS  FAIL:{len(results) - ok}")
    return 0 if ok == len(results) else 1


def _listen_yes_no(mic, timeout_s: float) -> str:
    """Offline Vosk yes/no listen for the break-resume confirm. Returns
    'yes' | 'no' | 'timeout'. NEVER depends on GandalfAI/Whisper -- the resume
    fires ~20 min after entry, when GandalfAI is normally re-asleep. Reads mic
    frames directly; the main loop owns the mic and is blocked in _run_break, so
    there is no concurrent reader. An explicit no-word wins over a yes-word in the
    same phrase ("no, not yet"). Silence -> 'timeout' -> the caller resumes."""
    import core.config as _cfg
    yes_words = set(getattr(_cfg, "BREAK_YES_WORDS", set()))
    no_words  = set(getattr(_cfg, "BREAK_NO_WORDS", set()))
    try:
        # _get_vosk_model() loads the model AND inserts _VOSK_PKG_PATH into
        # sys.path -- so it MUST run before `from vosk import ...` (mirrors the
        # barge-in path order in audio_io; the bare import fails otherwise on
        # first use, before any barge-in has primed sys.path).
        from hardware.audio_io import _get_vosk_model
        model = _get_vosk_model()
        if model is None:
            print("[BREAK] no Vosk model for confirm -- default resume", flush=True)
            return "timeout"
        from vosk import KaldiRecognizer
    except Exception as e:
        print(f"[BREAK] Vosk unavailable for confirm ({e}) -- default resume", flush=True)
        return "timeout"

    def _verdict(text: str):
        toks = (text or "").lower().split()
        if any(t in no_words for t in toks):    # explicit decline beats an assent
            return "no"
        if any(t in yes_words for t in toks):
            return "yes"
        return None

    try:
        rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(sorted(yes_words | no_words) + ["[unk]"]))
    except Exception as e:
        print(f"[BREAK] Vosk recognizer init failed ({e}) -- default resume", flush=True)
        return "timeout"

    # S240f: the break's own playback leaves the mic stopped, so this listen was
    # reading a dead stream and could never hear the answer -- live 2026-07-25 it
    # logged "[BREAK] confirm mic error ([Errno -9988] Stream closed)" and fell
    # through to verdict=timeout. Restart it first. A genuinely CLOSED stream
    # cannot be rebuilt from here (the caller owns the handle); that case is
    # recovered by the wakeword-loop reopen after _run_break() returns.
    try:
        if not mic.is_active():
            mic.start_stream()
    except Exception as _me:
        print(f"[BREAK] confirm mic restart failed ({_me})", flush=True)

    # Drain stale/overflowed frames + any residual quip bleed before decoding.
    for _ in range(int(SAMPLE_RATE / CHUNK * 0.3)):
        try: mic.read(CHUNK, exception_on_overflow=False)
        except Exception: break

    deadline = time.time() + max(2.0, timeout_s)
    while time.time() < deadline:
        try:
            data = mic.read(CHUNK, exception_on_overflow=False)
        except Exception as e:
            print(f"[BREAK] confirm mic error ({e})", flush=True)
            break
        _s = np.frombuffer(data, dtype=np.int16)
        if CHANNELS > 1:                          # Vosk wants mono
            _s = _s.reshape(-1, CHANNELS).mean(axis=1).astype(np.int16)
        try:
            if rec.AcceptWaveform(_s.tobytes()):
                txt = json.loads(rec.Result()).get("text", "")
            else:
                txt = json.loads(rec.PartialResult()).get("partial", "")
        except Exception:
            continue
        v = _verdict(txt)
        if v:
            print(f"[BREAK] confirm heard: {txt!r} -> {v}", flush=True)
            return v
    try:
        v = _verdict(json.loads(rec.FinalResult()).get("text", ""))
        if v:
            print(f"[BREAK] confirm (final): -> {v}", flush=True)
            return v
    except Exception:
        pass
    print("[BREAK] confirm timeout -- default resume", flush=True)
    return "timeout"


def _run_break(pa, teensy, leds, mic) -> None:
    """S202: voice-triggered quiet break ("do not disturb"). Full contract in the
    core.config 'Quiet break' block. Runs INLINE on the main loop thread, so the
    mic has exactly one reader and the wakeword is TRULY disabled for the window:
    wait_for_wakeword_or_button() forwards mic chunks to OpenWakeWord, and we
    never call it here, so OWW is fed nothing and cannot fire.

    Loop: enforced-quiet window (button / BREAK:CANCEL end it early) -> proactive
    wake + a couple of quips + reciprocal "shall I resume?" question -> red-pulse
    mic-active offline yes/no listen. 'yes' / any answer / silence -> resume;
    explicit 'no / not yet' -> re-arm another full window (operator's choice)."""
    import core.config as _cfg
    while True:
        dur = int(getattr(_cfg, "BREAK_DURATION_SECS", 1200))

        # ── Enter: acknowledge, sleep the face, swap to the amber break LED ──
        _play_break_cat("break_ack", "break_ack_emo", pa, teensy, leds)
        _do_sleep(teensy, leds)            # EYES:SLEEP + MOUTH:8 + sleep flag/state
        leds.show_break()                  # amber breathe overrides the sleep indigo
        state.break_until = time.time() + dur
        try: open("/tmp/iris_break_mode", "w").close()
        except Exception: pass
        print(f"[BREAK] Quiet break started: {dur}s (wakeword disabled)", flush=True)

        # ── Enforced quiet: mic NOT read -> OWW starved -> no wake. Button or a
        # WebUI BREAK:CANCEL (which zeroes break_until) ends it early. ──────────
        cancelled_early = False
        _last_hb = 0.0
        while True:
            now = time.time()
            if state.break_until <= 0.0:
                cancelled_early = True
                print("[BREAK] Cancelled via command", flush=True)
                break
            if now >= state.break_until:
                break
            if button_pressed():
                cancelled_early = True
                print("[BREAK] Cancelled via button", flush=True)
                _t0 = time.time()
                while button_pressed() and time.time() - _t0 < 3:
                    time.sleep(0.05)          # wait for release (debounce)
                break
            if now - _last_hb >= 20:
                _write_heartbeat("break")     # keep /api/health honest during the long block
                _last_hb = now
            time.sleep(0.2)

        # ── Proactive wake + resume sequence ────────────────────────────────
        try: os.remove("/tmp/iris_break_mode")
        except FileNotFoundError: pass
        except Exception: pass
        _do_wake(teensy, leds)             # EYES:WAKE + MOUTH:0 + clear sleep flag/state
        print(f"[BREAK] Window ended ({'early' if cancelled_early else 'timer'}) -- resuming", flush=True)

        _play_break_cat("break_resume", "break_resume_emo", pa, teensy, leds)
        _play_break_cat("break_resume_ask", "break_resume_ask_emo", pa, teensy, leds)

        # Red mic-active pulse + offline yes/no listen.
        leds.show_listen_confirm()
        verdict = _listen_yes_no(mic, float(getattr(_cfg, "BREAK_CONFIRM_TIMEOUT_SECS", 8.0)))
        leds.stop_anim()

        if verdict == "no":
            print("[BREAK] Declined -- re-arming another break window", flush=True)
            continue
        state.break_until = 0.0
        print(f"[BREAK] Resumed (verdict={verdict})", flush=True)
        show_idle_for_mode(leds)
        return


def _pre_synthesize_quips() -> bool:
    # S188: GATE the warm on GandalfAI/Kokoro reachability. An unguarded
    # synchronous warm here stalled the wakeword loop for minutes at boot when
    # GandalfAI was asleep-by-design (~100 failing TTS connects), and pinned the
    # GPU on every restart when it was up. Skip fast if unreachable; the
    # background warmer (_bg_quip_warm) retries once GandalfAI comes up.
    # Returns True if it actually warmed, False if skipped.
    if not gandalf_is_up():
        print("[QUIP] Warm skipped -- GandalfAI/Kokoro unreachable (will retry)", flush=True)
        return False
    from core.config import KOKORO_SPEED_QUIPS
    unique_wake = {l for _, _, _, lines in _WAKE_QUIPS for l in lines}
    unique_wake.add("Yeah.")   # _pick_wake_quip fallback (no/empty band for hour)
    for line in unique_wake:
        try:
            _wake_quip_cache[line] = synthesize(line, speed=KOKORO_SPEED_QUIPS)
            print(f"[QUIP] Cached: {line!r}", flush=True)
        except Exception as _e:
            print(f"[QUIP] Cache miss '{line}': {_e}", flush=True)

    # RD-047: sleep_window lines share the _rpqr_cache and _play_rpqr() player --
    # same shape (pre-cached PCM, explicit emotion), no new cache needed.
    rpqr_lines: list = (list(_DOUBLE_TAP_QUIPS) + list(_POST_SPEECH_QUIPS)
                        + list(_QCFG.get("sleep_window", []))
                        # S199 T2/T4: kid wake bank + mode-off sign-off share the
                        # RPQR cache and player, same pattern as sleep_window.
                        + list(_QCFG.get("kids_wake", []))
                        + list(_QCFG.get("kids_mode_off", []))
                        # S202: quiet-break entry ack + resume quips + resume Q.
                        # MUST be cached PCM -- the resume fires ~20 min after
                        # entry, when GandalfAI has re-slept and live synth fails.
                        + list(_QCFG.get("break_ack", []))
                        + list(_QCFG.get("break_resume", []))
                        + list(_QCFG.get("break_resume_ask", [])))
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

    # RD-047: second-stage fillers and the re-ask lines. Both must be cached PCM
    # -- a re-ask fires on the STT-failure path, where synthesizing on demand is
    # exactly the thing that might be broken.
    for line in _QCFG.get("kids_fillers_stage2", []):
        try:
            _kids_filler2_cache[line] = synthesize(line, speed=KOKORO_SPEED_QUIPS)
            print(f"[KIDFILL2] Cached: {line!r}", flush=True)
        except Exception as _e:
            print(f"[KIDFILL2] Cache miss '{line}': {_e}", flush=True)
    for line in _QCFG.get("kids_reask", []):
        try:
            _kids_reask_cache[line] = synthesize(line, speed=KOKORO_SPEED_QUIPS)
            print(f"[REASK] Cached: {line!r}", flush=True)
        except Exception as _e:
            print(f"[REASK] Cache miss '{line}': {_e}", flush=True)

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

    # S218: RPS throw beats, at GAME_COUNTDOWN_SPEED rather than the quip speed
    # -- the throw is the one place the tempo is the whole point. Warming here
    # is an optimisation only: _rps_throw_pcm() synthesises on demand for any
    # speed it has not seen, which is also what makes a live WebUI change to
    # GAME_COUNTDOWN_SPEED take effect without a restart.
    try:
        from core.config import GAME_COUNTDOWN_SPEED as _gcs, RPS_BEAT_PERIOD_S as _gbp
        _rps_throw_pcm(_gcs, _gbp)
    except Exception as _e:
        print(f"[GBEAT] Warm failed: {_e}", flush=True)
    return True


def _bg_quip_warm(retry_s: int = 60) -> None:
    """S188: Warm quip caches off the main thread, retrying until GandalfAI is
    reachable, so the wakeword loop is never blocked by TTS warmup at boot."""
    while True:
        try:
            if _pre_synthesize_quips():
                return
        except Exception as _e:
            print(f"[QUIP] Background warm error: {_e}", flush=True)
        time.sleep(retry_s)


# S192m AUD-12/B4: main-loop liveness heartbeat. A few writes per minute (one
# per wakeword-wait cycle + one per wake event), NOT per-frame -- respects the
# no-unbounded-logging rule (feedback_no_unbounded_logging). /tmp is RAM, so
# this is zero SD wear. Atomic write (temp file + os.replace) so a reader
# never sees a half-written file.
_HEARTBEAT_PATH = "/tmp/iris_heartbeat.json"
_heartbeat_loop_count = 0
_heartbeat_oww_restarts = 0


def _write_heartbeat(state: str) -> None:
    """Stamp /tmp/iris_heartbeat.json with the current loop state. state is
    'waiting' (blocked in wait_for_wakeword_or_button) or 'processing' (handling
    a wake/button trigger). Age = now - ts lets a reader (e.g. /api/health)
    tell a live loop apart from a stalled one regardless of which state it was
    last stamped in."""
    global _heartbeat_loop_count
    _heartbeat_loop_count += 1
    _tmp_path = _HEARTBEAT_PATH + f".tmp{os.getpid()}"
    try:
        with open(_tmp_path, "w") as _f:
            json.dump({
                "ts": time.time(),
                "state": state,
                "loop_count": _heartbeat_loop_count,
                "oww_restarts": _heartbeat_oww_restarts,
            }, _f)
        os.replace(_tmp_path, _HEARTBEAT_PATH)
    except Exception as _e:
        print(f"[HB] heartbeat write failed: {_e}", flush=True)


_resynth_in_progress = threading.Lock()


def _bg_reload_resynth(retry_s: int = 60) -> None:
    """S192k AUD-11: background re-synth for reload_soundboard(), same
    gate+thread+retry shape as _bg_quip_warm (S188). Runs off the CMD listener
    thread so a WebUI soundboard/config save can't block STOP/gesture handling
    behind ~100 synchronous TTS calls. Guarded by _resynth_in_progress so two
    reloads fired back-to-back (RELOAD_SOUNDBOARD + RELOAD_CONFIG, or two rapid
    WebUI saves) can't run concurrent re-synth passes."""
    if not _resynth_in_progress.acquire(blocking=False):
        print("[SOUNDBOARD] re-synth already in progress -- skipping duplicate reload", flush=True)
        return
    try:
        while True:
            try:
                if _pre_synthesize_quips():
                    print("[SOUNDBOARD] quips rebuilt + re-synthesized", flush=True)
                    return
            except Exception as _e:
                print(f"[SOUNDBOARD] quip reload failed: {_e}", flush=True)
                return
            time.sleep(retry_s)
    finally:
        _resynth_in_progress.release()


def reload_soundboard() -> None:
    """Re-read the soundboard after a WebUI save (CMD RELOAD_SOUNDBOARD): refresh
    the enabled clip set and rebuild quip caches in-process, so edits take effect
    without a service restart. Runs in the CMD listener thread; best-effort,
    never fatal. S192k AUD-11: the actual TTS re-synthesis (~100 Kokoro calls)
    is backgrounded via _bg_reload_resynth() -- same gandalf_is_up() gate +
    daemon-thread + retry shape as _bg_quip_warm (S188) -- so STOP/gesture
    handling on the CMD listener thread is never blocked behind it. Only the
    data-structure reload (fast) stays synchronous here."""
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
        _kids_filler2_cache.clear()   # F5 (MAD): new S197 caches were leaking stale
        _kids_reask_cache.clear()     #          PCM across WebUI soundboard edits
        _gesture_cue_cache.clear()
        threading.Thread(target=_bg_reload_resynth, name="reload-resynth", daemon=True).start()
    except Exception as _e:
        print(f"[SOUNDBOARD] quip reload failed: {_e}", flush=True)


# ── Conversation logger ───────────────────────────────────────────────────────

_convo_log_seq = 0


def flush_conversation_log(reason: str = "timeout"):
    global _convo_log_seq
    if not state.conversation_history:
        return
    import datetime
    # S224a: the Pi is a QUEUE, not a store. /media/root-rw is tmpfs and the SD is
    # mounted read-only, so a file appended here lives in RAM and dies at power-off
    # (measured 2026-07-21: conversations.jsonl was gone from RAM and SD alike).
    # One record per file into the outbox; scripts/iris_corpus_drain.py ships it to
    # the corpus server on GandalfAI and drops it only on ack. This path stays
    # purely local - no network call - so a sleeping GandalfAI can never slow a
    # conversation down.
    outbox = os.path.join(os.path.dirname(CONVERSATION_LOG), "outbox")
    os.makedirs(outbox, exist_ok=True)
    record = {
        "ts":       datetime.datetime.now().isoformat(timespec="seconds"),
        "reason":   reason,
        "mode":     "kids" if state.kids_mode else "adult",
        "model":    get_model(),
        "turns":    sum(1 for m in state.conversation_history if m["role"] == "user"),
        "messages": list(state.conversation_history),
    }
    # S224d: indices of assistant turns that answered a question about the past with
    # nothing retrieved. The corpus server refuses to extract episodes from these, so
    # an invented memory can never be recalled later as a real one. Absent key = old
    # behavior, so a record written by an older assistant.py still extracts normally.
    if _ungrounded_replies:
        record["ungrounded_recall_idx"] = [
            i for i, m in enumerate(state.conversation_history)
            if m.get("role") == "assistant" and m.get("content") in _ungrounded_replies]
        _ungrounded_replies.clear()
    # RD-060 provenance: which assistant turns were produced with a real captured
    # camera frame. Additive and optional in exactly the same way -- an absent key
    # means a record from an older assistant.py and changes no existing behavior.
    if _vision_replies:
        record["vision_reply_idx"] = [
            i for i, m in enumerate(state.conversation_history)
            if m.get("role") == "assistant" and m.get("content") in _vision_replies]
        _vision_replies.clear()
    _convo_log_seq += 1
    name = "%s-%d-%03d.json" % (datetime.datetime.now().strftime("%Y%m%dT%H%M%S"),
                                os.getpid(), _convo_log_seq)
    path = os.path.join(outbox, name)
    try:
        # tmp + replace so a crash mid-write cannot leave a half record for the drain
        with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
        print(f"[LOG]  Session queued ({record['turns']} turns, reason={reason}, {name})", flush=True)
    except Exception as e:
        print(f"[ERR]  Failed to queue conversation log: {e}", flush=True)


# ── Context timeout watchdog ──────────────────────────────────────────────────

def _context_watchdog():
    # Tunables read off the live core.config module each pass, not this module's
    # `from core.config import *` binding, which freezes VALUES at import and so
    # never sees a RELOAD_CONFIG (RD-047 follow-up).
    import core.config as _cfg
    if _cfg.CONTEXT_TIMEOUT_SECS <= 0:
        return
    _kids_carried_at = 0.0   # S201 (A4): last_interaction value already carried forward (re-fire guard)
    while True:
        time.sleep(30)
        try:
            _ctx_timeout = _cfg.CONTEXT_TIMEOUT_SECS
            _kids_timeout = _cfg.KIDS_MODE_INACTIVITY_TIMEOUT
            if state.last_interaction == 0.0:
                continue
            elapsed = time.time() - state.last_interaction
            _keep = _cfg.KIDS_HISTORY_TURNS * 2 if state.kids_mode else 0
            if elapsed >= _ctx_timeout and state.conversation_history:
                if _keep > 0:
                    # S201 (A4): kids carry-forward. Keep the last N exchanges across the
                    # context timeout so a child who returns within the 30-min kids window
                    # (KIDS_MODE_INACTIVITY_TIMEOUT) still gets continuity instead of a cold
                    # start. Guarded by _kids_carried_at (the last_interaction value already
                    # carried) so it fires ONCE per idle period, not every 30s tick -- else it
                    # would re-flush conversations.jsonl repeatedly while idle. flush still
                    # writes the FULL history for Session B's recall before the in-memory trim.
                    if _kids_carried_at != state.last_interaction:
                        flush_conversation_log(reason="timeout")
                        if len(state.conversation_history) > _keep:
                            state.conversation_history[:] = state.conversation_history[-_keep:]
                        _kids_carried_at = state.last_interaction
                        print(f"[CTX]  Kids carry-forward: kept last {_cfg.KIDS_HISTORY_TURNS} exchanges after {_ctx_timeout}s idle", flush=True)
                elif (_story_resume["pending"]
                      and _cfg.STORY_RESUME_WINDOW_SECS > 0
                      and time.time() - _story_resume["t"] < _cfg.STORY_RESUME_WINDOW_SECS):
                    # S217: story carry-forward. A truncated/interrupted story
                    # survives the context clear (last 2 exchanges = the ask +
                    # the told part) so "keep telling the story" within the
                    # resume window picks up where she stopped. Same one-shot
                    # guard as the kids branch so it fires once per idle period.
                    if _kids_carried_at != state.last_interaction:
                        flush_conversation_log(reason="timeout")
                        if len(state.conversation_history) > 4:
                            state.conversation_history[:] = state.conversation_history[-4:]
                        _kids_carried_at = state.last_interaction
                        print(f"[CTX]  Story carry-forward: kept last 2 exchanges after {_ctx_timeout}s idle", flush=True)
                else:
                    flush_conversation_log(reason="timeout")
                    state.clear_conversation()
                    _story_resume["pending"] = False   # S217: window expired with the clear
                    # Do NOT zero last_interaction here (F1 tail / MAD). clear_conversation()
                    # empties conversation_history, which already stops this branch re-firing.
                    # Zeroing the SHARED clock made the kids-mode auto-off below unreachable:
                    # the `if last_interaction == 0.0: continue` guard skipped every later
                    # tick, so with CONTEXT_TIMEOUT_SECS (300) < KIDS_MODE_INACTIVITY_TIMEOUT
                    # (1800) kids mode never reverted after a real conversation went idle.
                    # Leaving the clock running lets the 30-min revert fire as "30 min after
                    # the last interaction," every time.
                    print(f"[CTX]  Context cleared after {_ctx_timeout}s of silence", flush=True)
            if state.kids_mode and elapsed >= _kids_timeout:
                _set_kids_mode(False)
                flush_conversation_log(reason="kids_mode_timeout")
                state.clear_conversation()
                print(f"[MODE] Kids mode auto-off after {_kids_timeout}s inactivity", flush=True)
                # S199 T4: never-silent mode transition. This thread has no audio
                # handles, so ask the CMD listener (which does) to speak the
                # sign-off -- same self-UDP channel iris_sleep/wake.py already use.
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
                        _s.sendto(b"KIDS_OFF_QUIP", ("127.0.0.1", CMD_PORT))
                except Exception:
                    pass
        except Exception as e:
            print(f"[CTX]  watchdog error: {e}", flush=True)
            continue


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
    # S194 Rung6: say something while the brain boots, instead of a bare beep.
    speak_error("GANDALF_WAKING", kids=state.kids_mode)   # RD-047 kid register

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
    # S194 Rung6: don't fail silent -- tell the operator the brain didn't wake.
    speak_error("GANDALF_WAKE_FAIL", kids=state.kids_mode)   # RD-047 kid register
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
        while True:
            try:
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
                                elif cmd == "RELOAD_OVERRIDES":
                                    # S199 T7: light config reload -- re-bind
                                    # core.config globals only. No soundboard
                                    # cache clear or quip re-synth (that is
                                    # RELOAD_CONFIG's job; a re-synth per save
                                    # is the S188 GPU-burst class).
                                    print("[CMD] RELOAD_OVERRIDES", flush=True)
                                    import core.config as _cfg_mod
                                    _cfg_mod.reload_overrides()
                                elif cmd == "RELOAD_CONFIG":
                                    # S192b AUD-5: WebUI Voice-tab saves send this so a
                                    # KOKORO_VOICE/KOKORO_SPEED* change reaches the quip
                                    # cache without a service restart -- re-read
                                    # iris_config.json into core.config's live globals,
                                    # then reuse reload_soundboard()'s cache-clear +
                                    # re-synth (harmless extra soundboard-data reread).
                                    print("[CMD] RELOAD_CONFIG", flush=True)
                                    import core.config as _cfg_mod
                                    _cfg_mod.reload_overrides()
                                    reload_soundboard()
                                elif cmd == "RELOAD_KIDS_PROFILE":
                                    # RD-047: WebUI Kids-card profile save. Re-read
                                    # kids_profile.json into the module cache so the
                                    # next kids turn injects the new text. No restart.
                                    print("[CMD] RELOAD_KIDS_PROFILE", flush=True)
                                    try:
                                        from core import kids_profile as _kp
                                        _kids = _kp.reload().get("children", [])
                                        print(f"[KIDPROF] reloaded: "
                                              f"{[c['name'] for c in _kids]}", flush=True)
                                    except Exception as _ke:
                                        print(f"[KIDPROF] reload failed: {_ke}", flush=True)
                                elif cmd == "KIDS_OFF_QUIP":
                                    # S199 T4: sent by the context watchdog after a
                                    # kids-mode auto-off -- this thread holds the
                                    # audio handles the watchdog lacks.
                                    _speak_kids_mode_off(pa, teensy, leds)
                                elif cmd in ("KIDS:ON", "KIDS:OFF"):
                                    # RD-047: WebUI kids-mode toggle. Entry was
                                    # voice-only, which is fatal friction for a
                                    # kid-facing feature (plan 1D). Mirrors
                                    # handle_kids_mode_command()'s state changes.
                                    _want = (cmd == "KIDS:ON")
                                    if state.kids_mode != _want:
                                        _set_kids_mode(_want)
                                        flush_conversation_log(
                                            reason=f"mode_switch_kids_{'on' if _want else 'off'}")
                                        state.clear_conversation()
                                    print(f"[CMD] {cmd} -- kids_mode={state.kids_mode} "
                                          f"model={get_model()}", flush=True)
                                elif cmd == "BREAK:CANCEL":
                                    # S202: WebUI early-cancel of a quiet break.
                                    # _run_break()'s wait loop watches break_until;
                                    # zeroing it ends the window and jumps to the
                                    # proactive wake + resume-confirm.
                                    if state.break_until > 0.0:
                                        state.break_until = 0.0
                                        print("[CMD] BREAK:CANCEL -- ending quiet break early", flush=True)
                                    else:
                                        print("[CMD] BREAK:CANCEL -- no break active", flush=True)
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
                                        # S240e: an explicit wake also ENDS a quiet
                                        # break. Before this, waking during a break
                                        # lit the eyes while _run_break() kept the
                                        # mic starved for the rest of the window, so
                                        # the control looked like it worked and left
                                        # her deaf -- the operator hit exactly that
                                        # and had to restart the service to recover.
                                        # Cancelling here reuses the BREAK:CANCEL
                                        # mechanism (zero break_until; _run_break's
                                        # wait loop watches it) so the resume
                                        # sequence still runs normally.
                                        if state.break_until > 0.0:
                                            state.break_until = 0.0
                                            print("[CMD] EYES:WAKE -- also ending quiet break early", flush=True)
                                        _do_wake(teensy, leds)
                        except Exception as e:
                            print(f"[CMD] Listener error: {e}", flush=True)
            except Exception as e:
                print(f"[CMD] listener crashed: {e} -- retrying in 5s", flush=True)
                time.sleep(5)
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
                    "kids mode please", "activate kids mode", "children's mode on", "kid mode on",
                    # RD-047: a 6-year-old will not say "enable kids mode". Entry
                    # was adult-voice-only, which is fatal friction for a
                    # kid-facing feature. These are what a child actually says.
                    "iris kids mode", "play with me mode", "play with me")
    off_triggers = ("kids mode off", "kids mode stop", "disable kids mode", "turn off kids mode",
                    "deactivate kids mode", "kid mode off",
                    "exit kids mode", "leave kids mode", "stop kids mode", "quit kids mode",
                    "end kids mode", "no more kids mode",
                    "adult mode", "normal mode", "grown up mode", "grownup mode", "big kid mode",
                    "back to normal", "be normal", "talk normal")
    if any(tr in t for tr in on_triggers):
        _set_kids_mode(True)
        flush_conversation_log(reason="mode_switch_kids_on")
        state.clear_conversation()
        print(f"[MODE] Kids mode ON -- model: {OLLAMA_MODEL_KIDS}", flush=True)
        return "Kids mode activated.", True
    if any(tr in t for tr in off_triggers):
        _set_kids_mode(False)
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


def _speak_simple(reply, reply_chars, route, label, teensy, leds, pa, mic,
                   bench_stages, t_mono_wake, bench_transcript, gandalf_was_cold):
    """Synthesize a single already-decided reply and play it as one blob (no
    streaming), then write the bench row. Extracted from main() (S192e, audit
    AUD-2): six route handlers -- reflex sleep, kids-mode switch, volume,
    vision, utility, ambiguous sleep -- carried this exact block with only the
    reply text/char-count, bench route, and error-log label varying. Mirrors
    the original try/except boundary exactly: if synthesize() raises, nothing
    plays and the single [ERR] line below is the only output."""
    try:
        pcm_data = synthesize(reply)
        leds.show_speaking(); mic.stop_stream()
        _t_mono_play = time.monotonic()
        try: bench_stages["play_start_ms"] = round((_t_mono_play - t_mono_wake) * 1000)
        except Exception: pass
        play_pcm_speaking(pcm_data, pa, teensy); mic.start_stream()
        _bench_write(bench_stages, bench_transcript, reply_chars, get_model(), gandalf_was_cold, route, False)
    except Exception as e:
        print(f"[ERR]  TTS {label}: {e}", flush=True)


# ── LLM helpers ───────────────────────────────────────────────────────────────

# RD-064: _est_tokens() and the S226 _fit_context_budget() trim moved to
# core/prompt.py, where build_messages() calls them, so the voice path and the
# typed WebUI path share one token-budget implementation. Nothing else in this
# module referenced them. See core/prompt.py.


def _build_messages(num_predict: int = None) -> list:
    """Compose the messages list for Ollama /api/chat (the voice path).

    RD-064: the composition itself now lives in core.prompt.build_messages, shared
    byte-for-byte with the typed WebUI path so a spoken and a typed question are
    built identically -- same date stamp, kids notebook, personality tuning,
    trajectory clause, episodic recall, and S226 budget trim. This adapter only
    gathers the voice path's live state and hands it to the one composer:

      * history = state.conversation_history (the current user turn is already
        appended before we compose, so text is None and the stamp folds onto that
        last user turn -- the same copy-and-fold the old inline builder did);
      * the trajectory and recall clauses _route_predict refreshed a moment ago
        (passed as strings; build_messages applies the adult/non-game and
        not-mid-camera-game gates in one place);
      * the camera-game gate.

    The S134 no-system-message rule, the fragment ORDER, and the budget trim are
    all enforced in core.prompt now. See core/prompt.py --selftest for the guard
    that proves this path's output is byte-identical to the pre-RD-064 builder.
    """
    from core import prompt as _prompt
    _traj = ""
    try:
        _traj = _traj_current["clause"]
    except Exception as _tre:
        print(f"[TRAJ] clause read skipped: {_tre}", flush=True)
    _recall_cl = ""
    try:
        _recall_cl = _recall_current["clause"]
    except Exception as _rle:
        print(f"[RECALL] clause read skipped: {_rle}", flush=True)
    _weather_cl = ""
    try:
        _weather_cl = _weather_current["clause"]
    except Exception as _wce:
        print(f"[WX]   clause read skipped: {_wce}", flush=True)
    return _prompt.build_messages(
        None, state.conversation_history,
        "kids" if state.kids_mode else "adult",
        recall_clause=_recall_cl,
        weather_clause=_weather_cl,
        trajectory_clause=_traj,
        cam_game_active=_cam_game_state["active"],
        num_predict=num_predict,
    )


# S217 story resume: armed when a MAX-tier (story) turn ends truncated (token
# budget / TTS char cap) or interrupted, so a later "keep going" both routes to
# the story tier (_route_predict) and finds the story still in history (the
# context watchdog keeps the last 2 exchanges while this is pending). Cleared
# when a MAX-tier turn completes naturally or the resume window expires.
_story_resume = {"pending": False, "t": 0.0}

# Spoken when a story part hits its budget: a graceful chapter break instead of
# a dead stop. Ellipses are Kokoro's only pause lever (S194).
_STORY_BRIDGE_LINES = (
    "Ooh, there's more to this story... say keep going when you want the rest.",
    "That's a good place to pause... just say keep going and I'll carry on.",
    "And that's where I'll stop for now... want the rest? Just say keep going.",
)

# S221 (plan E): conversation-session state. Phase 1 keeps it deliberately
# minimal -- a turn counter + last-turn stamp feeding the persona length lean
# now and Phase 2's trajectory router later. No logging (RD-031); resets after
# a gap longer than the re-entry grace.
_convo_state = {"turns": 0, "t_last": 0.0}

# S221 (plan E) wind-down lines: the ONE spoken close when a conversation
# session ends (two silent windows or a polite dismissal). In IRIS's own voice
# -- dry British for adult, warm for kids -- name-blind, and short enough that a
# close never feels like another turn. Kokoro prosody rules (S194/S206): "..."
# is the only real pause lever, no em dashes, no asterisks (rendered as silence).
_CONVO_WINDUP_ADULT = (
    "Right, I'll leave you to it... shout if you want me.",
    "I'll let you get on then... you know where to find me.",
    "Back to standby... say my name when you need me.",
    "Off you pop... I'll be here, judging the furniture.",
    "That'll do for now... give me a shout whenever.",
)
_CONVO_WINDUP_KIDS  = (
    "Okay! I'll be right here if you want to chat more.",
    "I'll be waiting whenever you want to play... just say my name!",
    "Off you go then... come find me when you want another chat!",
)


# S227: _convo_lean_active() is gone with the lean it gated. CONVO_SESSION_ENABLED
# now governs ONE thing -- whether she holds the floor after a reply -- and reaches
# the prompt not at all. S225 had to switch off floor-holding to switch off a
# 370-character clause because the two shared this flag; they no longer do.


def _convo_note_turn():
    """S221 (plan E): count an adult non-game conversational LLM turn. Cheap by
    design (this is Phase 2's turns-in-session input); a silence gap longer
    than the re-entry grace starts a fresh session count."""
    import core.config as _cfg
    now = time.time()
    if _convo_state["t_last"] and now - _convo_state["t_last"] > max(_cfg.CONVO_REENTRY_GRACE_S, 60):
        _convo_state["turns"] = 0
    _convo_state["turns"] += 1
    _convo_state["t_last"] = now


def _speak_convo_windup(teensy, leds, pa, mic, kids):
    """S221 (plan E): one spoken closing line when a conversation session winds
    down (two silent windows, or a polite dismissal). Playback mirrors the
    follow-up time/volume fast-path (same synthesize + play_pcm_speaking
    pattern, mic left stopped for the post-loop mic.start_stream() recovery).
    Entirely non-fatal: a wind-down failure must never crash the loop."""
    try:
        line = random.choice(_CONVO_WINDUP_KIDS if kids else _CONVO_WINDUP_ADULT)
        emotion = "HAPPY"
        emit_emotion(teensy, leds, emotion)
        print("[FLWP] Wind-down line", flush=True)
        pcm_data = synthesize(line)
        leds.show_speaking(); mic.stop_stream()
        play_pcm_speaking(pcm_data, pa, teensy, emotion=emotion,
                          restore_mouth_idx=MOUTH_MAP.get(emotion, 0))
        _rpqr_state["t_last_spoke"] = time.time()
    except Exception as _we:
        print(f"[FLWP] Wind-down failed: {_we}", flush=True)


# S221 Phase 2 (plan C+F): per-turn trajectory plan -- DARK behind
# TRAJECTORY_ENABLED. _trajectory_prepare() runs at the TOP of _route_predict
# so every routed utterance gets a FRESH plan (a story early-return can never
# leave a stale clause behind); _build_messages folds the clause; the speed
# bias nudges only default-MEDIUM verdicts (services.llm.apply_speed_bias).
# With the flag off: state cleared, zero computation, prompts byte-identical.
_traj_current = {"speed": None, "clause": ""}

# S224c (RD-051 Phase D): this turn's episodic-recall clause. Refreshed at the top
# of _route_predict alongside the trajectory plan, folded by _build_messages into
# the CURRENT USER TURN. With RECALL_ENABLED off "clause" is never populated: no
# network call, prompts byte-identical to pre-S224c.
#
# "ungrounded" IS evaluated with the flag off (S228). The two fields deliberately
# no longer share a gate: the clause is a feature and stays behind its flag, the
# quarantine is a safety property and must not.
_recall_current = {"clause": "", "ungrounded": False, "spoken_override": ""}

# RD-068: this turn's weather clause. The router already detected the utterance
# and fetched the conditions (core/intent_router.py layer 2), so this slot is
# just where the payload it returned is parked for _build_messages to fold in --
# the same shape as the two clauses above, refreshed on EVERY turn from
# _result.payload right after classify(), so a weather turn can never leave a
# clause behind for the next question. "" whenever the utterance was not
# weather-shaped OR the fetch failed: services.weather fails closed, so nothing
# here is ever a stale reading.
_weather_current = {"clause": ""}

# Replies she gave to a recall question with NOTHING retrieved. These are quarantined
# from the corpus at flush time so a fabrication can never come back as a memory
# (S224d; see core/recall.py `last`). Bounded: cleared on every flush, and only ever
# holds replies from the current conversation.
_ungrounded_replies = set()

# RD-060 provenance: replies produced with a REAL captured camera frame in hand.
# Written to the record at flush as `vision_reply_idx`, the same additive shape as
# `ungrounded_recall_idx`. The useful question is the inverse of the obvious one:
# not "which replies saw something" but "which perception claims are in NEITHER
# set", because those were made with no camera evidence and no acknowledgement of
# it. Cleared on every flush; only ever holds this conversation's replies.
_vision_replies = set()


def _recall_prepare(text):
    """Look up an episodic memory for this utterance, or clear the slot.

    Never fatal. GandalfAI sleeps by design and S216 proved he can die mid-session,
    so every failure path here clears the clause and lets the turn proceed exactly
    as it does today -- a memory is never worth losing a reply for.
    """
    import core.config as _cfg
    _recall_current["clause"] = ""
    _recall_current["ungrounded"] = False
    _recall_current["spoken_override"] = ""
    try:
        from core import recall as _recall
    except Exception as _rie:
        print(f"[RECALL] module unavailable: {_rie}", flush=True)
        return
    # S228: THE QUARANTINE DECISION IS MADE BEFORE THE FLAG GUARD, and that
    # ordering is the entire fix. Until now this function returned at the
    # RECALL_ENABLED guard below BEFORE "ungrounded" was ever set, so nothing was
    # ever quarantined with recall off -- which inverted the intent exactly:
    # turning recall off to reduce risk was what poisoned the corpus for the next
    # time it was turned on. Measured live: the 2026-07-22 08:09:39 invention was
    # produced with recall off, went unquarantined, was indexed, came back at
    # 0.6783 twelve hours later, and she built a false narrative on it.
    #
    # This costs nothing when the flag is off: is_recall_question() is a string
    # match with no network call, and the injected stamp stays byte-identical
    # because "clause" is still never populated. The only thing that changes is
    # what the flushed record says about which turns must never become memories.
    try:
        _recall_current["ungrounded"] = _recall.is_recall_question(text)
    except Exception as _rqe:
        print(f"[RECALL] quarantine check skipped: {_rqe}", flush=True)
    if not getattr(_cfg, "RECALL_ENABLED", False):
        return
    try:
        _recall_current["clause"] = _recall.prepare(text)
        # QUARANTINE EVERY ANSWER TO A QUESTION ABOUT THE PAST, grounded or not.
        #
        # The first cut only quarantined UNGROUNDED answers. The n=3 voice bench
        # then caught the gap: handed a real memory that did not answer the
        # question ("how did the unicorn story end" retrieved, "what did the
        # doctor say about my knee" asked), she invented detailed medical advice
        # in 2 of 3 samples - RICE, wean off anti-inflammatories, he wanted a
        # scan. That turn is grounded=True, so the narrow rule would have indexed
        # it as a memory of medical advice nobody ever gave.
        #
        # Her answer about the past is never itself evidence about the past. It is
        # either a restatement of something already indexed, which adds nothing, or
        # it is invention, which adds something false. So none of it is indexed.
        _recall_current["ungrounded"] = bool(_recall.last.get("recall_question"))
        # RD-063 P3: the deterministic no-answer bank. When the ledger says the
        # record genuinely has nothing (or holds two conflicting answers), recall
        # decides the WHOLE reply in code and the model is not called at all.
        #
        # This is the one case where inference buys nothing and can only cost.
        # There is no memory to phrase, so the model's entire contribution would be
        # to word an admission - and the S224d bench measured it inventing on every
        # ungrounded prompt instead. A fixed line cannot invent. It is also faster,
        # which is a real gain on a turn that would otherwise be a full round trip
        # to GandalfAI for the word "no".
        _recall_current["spoken_override"] = _recall.last.get("spoken_override") or ""
    except Exception as _rce:
        print(f"[RECALL] lookup skipped: {_rce}", flush=True)


_QUESTION_STARTERS = frozenset((
    "what", "why", "how", "who", "where", "when", "which", "whose",
    "do", "does", "did", "can", "could", "is", "are", "am", "was", "were",
    "will", "would", "should", "shall", "have", "has"))


def _trajectory_prepare(text):
    """Compute this turn's trajectory plan (speed + steering clause), or clear
    it when the flag is off. Never fatal; failures clear the plan and log once
    per failure (same contract as the [TUNE]/[KIDPROF] injection skips)."""
    import core.config as _cfg
    _traj_current["speed"] = None
    _traj_current["clause"] = ""
    if not getattr(_cfg, "TRAJECTORY_ENABLED", False):
        return
    try:
        from core import trajectory as _traj
        words = _convo_state.setdefault("words", [])
        words.append(len(text.split()))
        del words[:-6]   # bounded trend window (RD-031)
        t = text.lower().strip()
        first = t.split()[0] if t.split() else ""
        is_q = t.rstrip(".!").endswith("?") or first in _QUESTION_STARTERS
        thread = ""
        if getattr(_cfg, "TRAJECTORY_THREADS_ENABLED", False):
            try:
                thread = (_traj.threads_from_log() or [""])[0]
            except Exception:
                thread = ""
        plan = _traj.plan_turn(_convo_state["turns"] + 1, list(words), is_q,
                               kids_mode=state.kids_mode,
                               game_active=_cam_game_state["active"],
                               thread=thread, seed=_traj.curiosity_seed())
        _traj_current["speed"] = plan["speed"] if plan["clause"] else None
        _traj_current["clause"] = plan["clause"]
        if getattr(_cfg, "TRAJECTORY_DEBUG", False):
            print(f"[TRAJ] turns={_convo_state['turns'] + 1} speed={plan['speed']} "
                  f"direction={plan['direction']} thread={'y' if thread else 'n'}", flush=True)
    except Exception as _tje:
        print(f"[TRAJ] plan skipped: {_tje}", flush=True)


def _route_predict(text):
    """
    S217: tier routing with story-resume awareness. Normal utterances get the
    classifier verdict; while a truncated story is pending, bare continuation
    phrases ("keep going", "then what", ...) are promoted to the MAX story tier
    so the resumed part gets a full story budget.
    """
    import core.config as _cfg
    _trajectory_prepare(text)   # S221 Phase 2: fresh plan per routed utterance (inert when dark)
    _recall_prepare(text)       # S224c Phase D: fresh memory lookup per routed utterance (inert when dark)
    _np = classify_response_length(text)
    if (_story_resume["pending"] and _np < _cfg.NUM_PREDICT_MAX
            and is_story_continuation(text)):
        print("[STORY] Continuation of pending story -- promoting to MAX tier", flush=True)
        return _cfg.NUM_PREDICT_MAX
    if _traj_current["speed"]:
        # S221 Phase 2 (dark): trajectory speed bias -- only default-MEDIUM
        # verdicts ever move (explicit SHORT/LONG/MAX always win).
        from services.llm import apply_speed_bias
        _np_b = apply_speed_bias(_np, _traj_current["speed"])
        if _np_b != _np and getattr(_cfg, "TRAJECTORY_DEBUG", False):
            print(f"[TRAJ] tier bias {_np} -> {_np_b} ({_traj_current['speed']})", flush=True)
        _np = _np_b
    return _np


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

    # ── RD-063 P3: the deterministic no-answer bank ──────────────────────────
    # The ledger said the record genuinely holds nothing on this (or holds two
    # conflicting answers). Speak a code-decided line and DO NOT call the model.
    #
    # Placed after the user turn is appended to history, so the conversation reads
    # normally afterwards and a follow-up ("it was Tuesday") lands in context the
    # same way it would have. Everything below this point is skipped, which is the
    # point: there is no memory to phrase, so the model's only possible
    # contribution is to word an admission, and the S224d bench measured it
    # inventing on exactly that prompt shape instead.
    _override = _recall_current.get("spoken_override") or ""
    if _override:
        print("[RECALL] no-answer bank: fixed line, model NOT called", flush=True)
        try:
            _emotion = "NEUTRAL"
            emit_emotion(teensy, leds, _emotion)
            _pcm = synthesize(_override)
            leds.show_speaking(); mic.stop_stream()
            play_pcm_speaking(_pcm, pa, teensy, emotion=_emotion,
                              restore_mouth_idx=MOUTH_MAP.get(_emotion, 0))
            _rpqr_state["t_last_spoke"] = time.time()
            state.conversation_history.append({"role": "assistant",
                                               "content": _override})
            # Quarantined like any other answer about the past. It is not a memory
            # and must never be indexed as one (S224d) -- doubly so here, since a
            # bank line indexed as an episode would teach retrieval that "I have
            # nothing on that" is the answer to the question that was asked.
            _ungrounded_replies.add(_override)
            return _override, _emotion, False, True
        except Exception as _oe:
            # Never lose the turn over this. Fall through to the model, which is
            # exactly the pre-P3 behavior.
            print(f"[RECALL] bank playback failed, using the model: {_oe}", flush=True)

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
    _capped = False       # S217: TTS_MAX_CHARS halted dispatch mid-story
    _stream_meta = {}     # S217: stream_ollama fills done_reason ("length" = token cap)

    # Fresh turn: producer owns the _stop_playback lifecycle on the
    # streaming path (play_pcm_stream no longer clears it). A STOP routed
    # while idle would otherwise falsely abort this turn's first chunk.
    _stop_playback.clear()

    # Kids gap-filler (S144): if first real audio is late, drop one short
    # playful "thinking" clip into the silence so a low-attention child stays
    # engaged. Serialized against the real player via _filler_lock so the two
    # never touch the audio device at once. Main turn only (not follow-ups).
    _filler_lock = threading.Lock()
    # Read the filler tunables off the live module, not this module's frozen
    # import binding, so a WebUI RELOAD_CONFIG actually reaches them (RD-047).
    import core.config as _cfg_live
    if (stage_prefix == "" and state.kids_mode and _cfg_live.KIDS_GAP_FILLERS
            and _kids_filler_cache):
        def _kids_gap_filler():
            import random

            def _wait_then_play(cache, deadline, tag) -> bool:
                """Wait until `deadline` (monotonic), then play one cached line
                under _filler_lock. False if the turn moved on meanwhile."""
                while time.monotonic() < deadline:
                    if (_player_thread is not None or _stop_playback.is_set()
                            or _player_interrupted.is_set()):
                        return False
                    time.sleep(0.05)
                with _filler_lock:
                    # Re-check under the lock: the real player may have started
                    # while we were waiting on the lock.
                    if (_player_thread is not None or _stop_playback.is_set()
                            or _player_interrupted.is_set()):
                        return False
                    if not cache:
                        return False
                    _line = random.choice(list(cache.keys()))
                    _pcm_fill = cache.get(_line)
                    if not _pcm_fill:
                        return False
                    try:
                        play_pcm_speaking(_pcm_fill, pa, teensy,
                                          emotion="CURIOUS", restore_mouth_idx=0)
                        print(f"[{tag}] {_line!r}", flush=True)
                        return True
                    except Exception as _fe:
                        print(f"[{tag}] failed: {_fe}", flush=True)
                        return False

            _t0 = time.monotonic()
            if not _wait_then_play(_kids_filler_cache,
                                   _t0 + _cfg_live.KIDS_THINK_FILLER_MS / 1000.0,
                                   "KIDFILL"):
                return
            # RD-047 second stage: a genuinely slow turn (cold GandalfAI, long
            # reply) leaves 4s+ of silence AFTER the first filler. Deadline is
            # measured from the same _t0, but never less than 1s after stage 1's
            # audio actually finished, so the two can't play back-to-back.
            _ms2 = _cfg_live.KIDS_THINK_FILLER2_MS
            if (not _ms2 or _ms2 <= _cfg_live.KIDS_THINK_FILLER_MS
                    or not _kids_filler2_cache):
                return
            _wait_then_play(_kids_filler2_cache,
                            max(_t0 + _ms2 / 1000.0, time.monotonic() + 1.0),
                            "KIDFILL2")
        threading.Thread(target=_kids_gap_filler, daemon=True).start()

    def _run_player(_emotion):
        # bench_stages is the stats sink for P4 gap telemetry (S192f/D1): the
        # player fills gap_count/gap_total_ms/gap_max_ms/blobs_played into it at
        # drain, and the caller's _bench_write serializes the whole dict.
        try:
            _player_result["interrupted"] = play_pcm_stream(
                _pcm_q, pa, teensy, emotion=_emotion,
                restore_mouth_idx=MOUTH_MAP.get(_emotion, 0),
                interrupted=_player_interrupted, stats=bench_stages)
        except Exception as e:
            print(f"[ERR]  player thread crashed: {e}", flush=True)
            _player_result["interrupted"] = True

    try:
        for chunk, chunk_emotion in stream_ollama(
            _build_messages(num_predict), get_model(), num_predict, meta=_stream_meta
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
            # S199 T7: read off the live module -- the star-imported binding is
            # frozen at boot (S197b class), so a WebUI TTS_MAX_CHARS save never
            # reached this cap until a restart.
            if _tts_chars >= _cfg_live.TTS_MAX_CHARS:
                print(f"[TTS]  Utterance cap reached: {_tts_chars} chars dispatched >= TTS_MAX_CHARS={_cfg_live.TTS_MAX_CHARS} -- halting stream", flush=True)
                _capped = True
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

            # Synthesize this sentence. synthesize_captioned() returns
            # (pcm, mouth_timeline): the timeline comes from Kokoro's real
            # per-word timestamps (RD-044) and is None when word timing is
            # unavailable (F5 active / Kokoro down / captioned failed), in which
            # case the player falls back to the legacy fixed-timer mouth. It only
            # raises if TTS fully fails; then skip this sentence, not the turn.
            try:
                _pcm, _mouth_tl = synthesize_captioned(chunk)
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
                    bench_stages["engine"] = "kokoro" if _cfg_live.KOKORO_ENABLED else "piper"
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

            _pcm_q.put((_pcm, _mouth_tl))
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

    # S217: story-tier truncation handling. If a MAX-tier (story) part was cut
    # by the token budget or the TTS char cap -- and the user didn't STOP her --
    # speak a short bridge instead of dying mid-arc, so the ending is graceful
    # and teaches the resume phrase. The bridge joins reply_parts so history
    # reflects what was actually said.
    _story_tier = (_tier == "MAX") or (num_predict >= _cfg_live.NUM_PREDICT_MAX)
    _truncated = _capped or _stream_meta.get("done_reason") == "length"
    if (_story_tier and _truncated and not _interrupted
            and reply_parts and _player_thread is not None):
        _bridge = random.choice(_STORY_BRIDGE_LINES)
        try:
            _b_pcm, _b_tl = synthesize_captioned(_bridge)
            _pcm_q.put((_b_pcm, _b_tl))
            reply_parts.append(_bridge)
        except Exception as _be:
            print(f"[ERR]  story bridge TTS failed: {_be}", flush=True)
        print(f"[STORY] Part truncated ({'tts_cap' if _capped else 'token_budget'}) -- bridge spoken, resume armed", flush=True)

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
        _player_thread.join(timeout=180)
        if _player_thread.is_alive():
            print("[ERR]  player thread failed to drain in 180s -- abandoning turn", flush=True)
            _interrupted = True
        _interrupted = _player_result["interrupted"] or _interrupted
    elif not reply:
        print("[LLM]  Empty reply -- nothing to play", flush=True)

    # S217: finalize story-resume on story-tier turns only. Truncated OR
    # interrupted (a "stop" mid-story should still be resumable later) arms it;
    # a naturally completed story clears it. Other tiers never touch it.
    if _story_tier:
        _story_resume["pending"] = bool(reply_parts) and (_truncated or _interrupted)
        _story_resume["t"] = time.time()

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
    if _recall_current.get("ungrounded") and reply:
        # Quarantine, not deletion: she still SAYS it, and the raw transcript still
        # records it. It simply never becomes a retrievable memory (S224d).
        _ungrounded_replies.add(reply)
        if len(_ungrounded_replies) > 40:      # RD-031: bounded, never a leak
            _ungrounded_replies.clear()
    if len(state.conversation_history) > 20:
        state.conversation_history.pop(0); state.conversation_history.pop(0)
        if not (state.conversation_history and state.conversation_history[0].get("content", "").startswith("[Earlier conversation")):
            state.conversation_history.insert(0, {"role": "assistant", "content": "[Earlier conversation omitted]"})

    _no_audio = bool(reply) and _player_thread is None and not _interrupted
    if _no_audio:
        print("[ERR]  turn produced no audio (all TTS failed) -- reporting failure", flush=True)
    return reply, _current_emotion, _interrupted, (not _no_audio)


# ── Kids camera games (S144, reworked S210) ──────────────────────────────────
# Each reply ends by asking the child something, so the follow-up loop engages
# and the child can respond without re-saying the wake word. S210: I_SPY and
# RPS verdicts/lines are decided in code (core/camera_games) with the vision
# model used for perception only; SHOW_ME/FACE keep the free-text vision turn.
# S210: I_SPY no longer uses a free-text vision prompt. The old "secretly pick
# an object" call never stored the pick, so the text-LLM judge of every guess
# never knew the answer -- the game's confusion root cause. I Spy now runs
# ask_ispy_pick() (structured JSON pick, stored in _cam_game_state["data"]) and
# all guesses are judged in code (core/camera_games). SHOW_ME/FACE unchanged.
_CAMERA_GAME_PROMPTS = {
    "SHOW_ME": ("A young child is holding something up to your camera. "
                "Look at the image and playfully guess what the object is in one short "
                "sentence, in character as IRIS the fun kids robot. Then ask if you got it right."),
    "FACE": ("A young child is making a face at your camera. Look at the image and "
             "playfully guess what feeling or expression they are showing, in one short "
             "sentence, in character as IRIS. Then invite them to try another face."),
    # S219: "commit to a real guess" is the whole design. Quick, Draw! showed the
    # fun is a confident wrong guess, not a hedge -- "is it a dog? no? a COW!"
    # is a laugh, while "I am not sure what that is" ends the game. A child's
    # drawing is ALWAYS rough, so hedging would be the default without this.
    "DRAW": ("A young child is holding up a drawing they made. Look at the image and "
             "playfully say what you think it shows, in one short sentence, in character "
             "as IRIS the fun kids robot. Always commit to a real, specific guess even if "
             "the drawing is rough or wobbly, and never say you cannot tell. Be delighted "
             "by it. Then ask if you got it right."),
}
_CAMERA_GAME_FALLBACKS = {
    "I_SPY":   "Uh oh, my camera's being shy! Want to play a riddle instead?",
    "SHOW_ME": "Hmm, my camera blinked! Hold it up again, or want a different game?",
    "FACE":    "Aw, my camera missed it! Make the face again, or want to try I Spy?",
    # S219: every camera-game failure line names a PHYSICAL cause the child can
    # act on. The Cozmo child-play study found kids happily work around a
    # camera's limits (reposition, fix the light) when the miss is attributed to
    # something physical, and decide the robot is being stubborn when it isn't.
    "DRAW":    "Oh no, my camera blinked! Hold the picture up close to my eye and keep it still?",
}
_CAMERA_GAME_PROMPTS_ADULT = {
    "SHOW_ME": ("Someone is holding something up to your camera. "
                "Look at the image and guess what the object is in one short "
                "sentence, in character as IRIS. Then ask if you got it right."),
    "FACE": ("Someone is making a face at your camera. Look at the image and "
             "guess what feeling or expression they are showing, in one short "
             "sentence, in character as IRIS. Then invite them to try another face."),
    "DRAW": ("Someone is holding up a drawing they made. Look at the image and "
             "say what you think the drawing shows, in one short sentence, in "
             "character as IRIS. Commit to a real guess even if the drawing is "
             "rough, and be warm about it. Then ask if you got it right."),
}

# Pre-cached intro lines (S168 Break 1): spoken immediately after the wake ack
# while the blocking capture + vision inference runs, so there's no dead air.
_CAMERA_GAME_INTROS = {
    "I_SPY":   "Ooh, I Spy! Let me take a peek...",
    "SHOW_ME": "Ooh, let me see what you've got...",
    "FACE":    "Ooh, let me look at that face...",
    "DRAW":    "Ooh, a drawing! Hold it up close and let me squint at it...",
    # S210: not a game key -- cached filler played while the RPS hand classify
    # runs, so the post-SHOOT beat is never dead air.
    # S218: was "Let's seeee...", which the operator heard as "let's sayyya".
    # Kokoro is grapheme-driven (the same reason core/normalize_tts.py has to map
    # "Ava" to "May"), so an invented elongated spelling gets phonemised as the
    # nonsense word it literally is. Written normally, it says what it means.
    "RPS_PEEK": "Let's see...",
}

# Follow-up vision prompts (S168 Break 7): for SHOW_ME / FACE the child's frame
# changes between turns, so each guess re-captures and re-asks vision with the
# guess text spliced in. I_SPY needs no re-capture (the spied object is fixed);
# its guesses are code-judged against the stored pick (S210).
_CAMERA_GAME_FOLLOWUP_PROMPTS = {
    "SHOW_ME": ("A young child is playing a guessing game holding something up to "
                "your camera. They just said: '{guess}'. Look at the image again and "
                "react playfully in character as IRIS the fun kids robot -- tell them "
                "if they're right, or give a fun hint and guess again. One short "
                "sentence, then invite their next guess."),
    "FACE": ("A young child is playing a face-making game. They just said: '{guess}'. "
             "Look at the image again and react playfully in character as IRIS to the "
             "face they're making now. One short sentence, then invite another face."),
    "DRAW": ("A young child drew a picture and you guessed what it was. They just "
             "said: '{guess}'. Look at the image again and react in character as IRIS "
             "the fun kids robot -- if you were right, be thrilled; if you were wrong, "
             "be delighted to be told and say what you can see now that you know. One "
             "short sentence, then ask them to draw you another one."),
}
_CAMERA_GAME_FOLLOWUP_PROMPTS_ADULT = {
    "SHOW_ME": ("Someone is playing a guessing game holding something up to your "
                "camera. They just said: '{guess}'. Look at the image again and react "
                "in character as IRIS -- tell them if they're right, or give a hint and "
                "guess again. One short sentence, then invite their next guess."),
    "FACE": ("Someone is playing a face-making game. They just said: '{guess}'. Look "
             "at the image again and react in character as IRIS to the face they're "
             "making now. One short sentence, then invite another face."),
    "DRAW": ("Someone drew a picture and you guessed what it was. They just said: "
             "'{guess}'. Look at the image again and react in character as IRIS -- if "
             "you were right, enjoy it; if you were wrong, take it well and say what you "
             "can see now that you know. One short sentence, then invite another drawing."),
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
    _cam_game_state["data"] = {}
    if img is None:
        reply = _CAMERA_GAME_FALLBACKS.get(game, "My camera's being shy! Want a different game?")
    elif game == "I_SPY":
        # S210: structured pick, STORED, so every guess is judged in code
        # against a known answer (see the _CAMERA_GAME_PROMPTS note). The
        # start line states the roles (I'm the spy, you're the finder) and
        # gives only a coarse-attribute clue: confidence via vagueness.
        pick = ask_ispy_pick(img, get_model())
        if pick is None:
            reply = _CAMERA_GAME_FALLBACKS["I_SPY"]
        else:
            clues = build_ispy_clues(pick)
            _cam_game_state["data"] = {
                "secret":          pick["object"],
                "synonyms":        pick["synonyms"],
                "clues":           clues,
                "clue_idx":        0,
                "awaiting_replay": False,
            }
            reply = ispy_start_line(clues[0], kids=state.kids_mode)
            emotion = "CURIOUS"
            print(f"[GAME] I Spy pick: secret={pick['object']!r} "
                  f"syn={pick['synonyms']} clues={clues}", flush=True)
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


def _speak_game_line(reply, emotion, teensy, leds, pa, mic, user_text=None, history=True,
                     speed=None):
    """S210: speak a CODE-DECIDED game line (no LLM round trip -- guess verdicts
    and RPS results are instant, which is where the game feel lives). Appends
    history unless history=False (countdowns are noise), mirrors the
    _play_camera_game TTS block. Returns interrupted.

    S216: `speed` overrides KOKORO_SPEED for this utterance only (the RPS
    countdown needs a slower throw rhythm than her normal speech). Synthesis was
    measured at 0.18 s for a countdown line, so this stays a live synth -- no
    cache needed to afford it."""
    if user_text:
        state.conversation_history.append({"role": "user", "content": user_text})
    _interrupted = False
    try:
        pcm_data = synthesize(reply, speed=speed)
        emit_emotion(teensy, leds, emotion)
        leds.show_speaking(); mic.stop_stream()
        _interrupted = play_pcm_speaking(pcm_data, pa, teensy, emotion=emotion,
                                         restore_mouth_idx=MOUTH_MAP.get(emotion, 0))
    except Exception as e:
        print(f"[ERR]  Game line TTS: {e}", flush=True)
    finally:
        try: mic.start_stream()
        except OSError: pass
    if history:
        state.conversation_history.append({"role": "assistant", "content": reply})
        if len(state.conversation_history) > 20:
            state.conversation_history.pop(0); state.conversation_history.pop(0)
            if not (state.conversation_history and state.conversation_history[0].get("content", "").startswith("[Earlier conversation")):
                state.conversation_history.insert(0, {"role": "assistant", "content": "[Earlier conversation omitted]"})
    _rpqr_state["t_last_spoke"] = time.time()
    return _interrupted


# S218 throw-rhythm constants. _RPS_PCM_RATE tracks play_pcm_speaking's `rate`
# default and services/tts's 48 kHz mono request -- the throw blob is built by
# hand, so it has to agree with both. _RPS_BEAT_FLOOR_S keeps a beat that is
# longer than the whole period from running straight into the next word.
_RPS_PCM_RATE     = 48000
_RPS_BEAT_FLOOR_S = 0.12


def _trim_pcm_silence(pcm, thresh: int = 300):
    """Strip leading/trailing near-silence from an s16le mono blob.

    Kokoro pads every utterance, and MEASURED on the Pi the padding is both
    large and INCONSISTENT: 0.05 s lead and 0.26-0.34 s tail across the four
    throw words. Concatenating untrimmed beats would therefore bake a different
    dead spot after each word, which is the exact unevenness this whole change
    exists to remove. Trim first, then lay the beats on a metronome."""
    try:
        a = np.frombuffer(pcm, dtype='<i2')
        idx = np.nonzero(np.abs(a) > thresh)[0]
        if len(idx) == 0:
            return pcm
        return a[idx[0]:idx[-1] + 1].tobytes()
    except Exception as _e:
        print(f"[GAME] RPS beat trim failed: {_e}", flush=True)
        return pcm


def _rps_throw_pcm(speed, period):
    """The whole throw as ONE cached PCM blob: the four beats trimmed to their
    audible content and laid out so each ONSET falls exactly `period` seconds
    after the last.

    Onset spacing, not gap size, is what the ear reads as rhythm -- "Scissors."
    is 0.15 s longer than "Rock.", so a constant GAP produces a measurably
    uneven countdown while a constant PERIOD produces a metronome.

    One blob rather than four playbacks is deliberate: play_pcm_speaking opens a
    stream and sleeps 0.35 s to settle the mouth on EVERY call, so playing the
    beats separately would have added ~1.4 s of dead air to a ~3.7 s countdown
    and put the settle gaps in the middle of the rhythm. Cached per
    (speed, period) so a live WebUI change to either takes effect on the next
    match with no restart."""
    key = (round(float(speed), 2), round(float(period), 2))
    pcm = _rps_beat_cache.get(key)
    if pcm is not None:
        return pcm
    parts = []
    for _b in RPS_THROW_BEATS:
        try:
            parts.append(_trim_pcm_silence(synthesize(_b, speed=key[0])))
        except Exception as _e:
            print(f"[GAME] RPS beat synth failed {_b!r}: {_e}", flush=True)
            return None                      # never cache a partial throw
    out = bytearray()
    _onsets = []
    for _i, _p in enumerate(parts):
        _onsets.append(len(out) / (_RPS_PCM_RATE * 2))
        out += _p
        if _i == len(parts) - 1:
            break
        _pad = key[1] - len(_p) / (_RPS_PCM_RATE * 2)
        if _pad < _RPS_BEAT_FLOOR_S:
            print(f"[GAME] RPS beat {RPS_THROW_BEATS[_i]!r} is longer than the "
                  f"{key[1]:.2f}s period -- rhythm will be uneven. Raise "
                  f"RPS_BEAT_PERIOD_S or raise GAME_COUNTDOWN_SPEED.", flush=True)
            _pad = _RPS_BEAT_FLOOR_S
        out += b"\x00" * (int(_RPS_PCM_RATE * _pad) * 2)
    pcm = bytes(out)
    _rps_beat_cache[key] = pcm
    print(f"[GAME] RPS throw built: speed={key[0]} period={key[1]}s "
          f"onsets={[round(o, 2) for o in _onsets]} "
          f"total={len(pcm) / (_RPS_PCM_RATE * 2):.2f}s", flush=True)
    return pcm


def _play_rps_throw(teensy, leds, pa, mic, speed):
    """Play the metronomic throw. Returns True if a barge-in interrupted it.

    Why any of this exists: S216 spelled the beats out as six-dot ellipses in
    one utterance and slowed synthesis down, and the operator's play-test found
    the result audibly uneven with a long hang between "paper" and "scissors".
    An ellipsis is the only pause lever Kokoro honours, but nothing specifies
    its LENGTH, so punctuation cannot be a metronome. Silence measured in
    samples can."""
    import core.config as _cfg
    period = float(getattr(_cfg, "RPS_BEAT_PERIOD_S", 1.0))
    pcm = _rps_throw_pcm(speed, period)
    if not pcm:
        return False
    _interrupted = False
    try:
        leds.show_speaking(); mic.stop_stream()
        _interrupted = play_pcm_speaking(pcm, pa, teensy, emotion="CURIOUS",
                                         restore_mouth_idx=0)
    except Exception as _e:
        print(f"[GAME] RPS throw playback failed: {_e}", flush=True)
    finally:
        try: mic.start_stream()
        except OSError: pass
    return _interrupted


def _play_rps_round(user_text, first, teensy, leds, pa, mic, bench_stages, t_mono0):
    """S210: one Rock Paper Scissors round, self-contained (it cannot ride the
    speech follow-up loop: after "SHOOT!" the player is holding up a hand, not
    talking). Fairness is structural: the code commits IRIS's move BEFORE any
    frame is captured (and the intro says so), the vision model classifies ONLY
    the player's hand, and the winner is decided in code -- the LLM never
    judges. Unclear hand / failed capture gets one in-round retry with
    coaching, then a graceful no-score skip. Returns the same
    (reply, emotion, interrupted, ok) tuple as _speak_llm_turn."""
    # S216 tempo: this now plays a whole MATCH, not one round, when
    # RPS_AUTO_CONTINUE is on. Live measurement (2026-07-20) put round-to-round
    # at ~21 s for ~1 s of play, and ~5 s of that was a full listen + endpoint
    # cycle spent collecting a "yes"/"Continue." the player was always going to
    # give. Rounds now roll straight on; the mid-match result line ends flat so
    # nothing invites an answer. Exits stay available: a barge-in ("stop") ends
    # the match, two blind rounds in a row hand the turn back, and the
    # match-ending line asks "Rematch?" through the normal follow-up loop.
    # Tunables are read off the live core.config module (not this module's
    # import-time binding) so a WebUI save applies without a restart.
    import core.config as _cfg
    _auto = bool(getattr(_cfg, "RPS_AUTO_CONTINUE", True))
    _max_rounds = int(getattr(_cfg, "RPS_MAX_ROUNDS", 9)) if _auto else 1
    _cd_speed = float(getattr(_cfg, "GAME_COUNTDOWN_SPEED", 0.80))
    print(f"[GAME] RPS match (first={first} auto={_auto} max_rounds={_max_rounds})", flush=True)
    state.last_interaction = time.time()
    d = _cam_game_state["data"]
    if first:
        d.clear(); d.update(kid=0, iris=0)
    emit_emotion(teensy, leds, "CURIOUS")
    _intro = first          # the long explainer countdown, once per match
    _skips = 0              # consecutive rounds where the hand was never read
    reply, emotion, _interrupted = "", "HAPPY", False
    for _round in range(1, _max_rounds + 1):
        # Commit IRIS's move BEFORE the frame exists, every round (S210
        # fairness: the countdown says she cannot cheat, and this is why).
        iris_move = rps_pick_move()
        kid_move = "unclear"
        for _attempt in (1, 2):
            line = (rps_countdown_line(_intro, state.kids_mode) if _attempt == 1
                    else rps_unclear_line(state.kids_mode))
            # S218: the lead-in and the throw are now two separate playbacks.
            # The lead-in is prose and stays one utterance; the throw is four
            # cached beats with real gaps, which is the only way to get an even
            # rhythm out of an engine with no rhythm control (see
            # _play_rps_throw). Both run at GAME_COUNTDOWN_SPEED.
            _speak_game_line(line, "CURIOUS", teensy, leds, pa, mic,
                             user_text=user_text if (_attempt == 1 and _round == 1) else None,
                             history=False, speed=_cd_speed)
            _play_rps_throw(teensy, leds, pa, mic, _cd_speed)
            _intro = False
            img = capture_image()
            if img is None:
                print(f"[GAME] RPS r{_round} attempt {_attempt}: capture failed", flush=True)
                continue
            # Overlap the classify call with the cached "let's seeee" filler so the
            # post-SHOOT beat is never silent (same pattern as the intro filler).
            _res = {"m": "unclear"}
            def _cls():
                _res["m"] = ask_rps_hand(img, get_model())
            _t = threading.Thread(target=_cls, daemon=True); _t.start()
            _peek = _game_intro_cache.get("RPS_PEEK")
            if _peek and _t.is_alive():
                try:
                    leds.show_thinking(); mic.stop_stream()
                    play_pcm_speaking(_peek, pa, teensy, emotion="CURIOUS", restore_mouth_idx=0)
                except Exception as _pe:
                    print(f"[GAME] RPS peek filler failed: {_pe}", flush=True)
                finally:
                    try: mic.start_stream()
                    except OSError: pass
            leds.show_thinking()
            _t.join(timeout=90)  # ask_rps_hand carries its own VISION_TIMEOUT
            kid_move = _res["m"]
            print(f"[GAME] RPS r{_round} attempt {_attempt}: "
                  f"kid={kid_move} iris={iris_move}", flush=True)
            if kid_move in RPS_MOVES:
                break
        _more = _auto and _round < _max_rounds
        if kid_move not in RPS_MOVES:
            _skips += 1
            _hand_back = _skips >= 2 or not _more
            reply, emotion = rps_skip_line(state.kids_mode,
                                           continuing=not _hand_back), "CONFUSED"
            match_over = False
        else:
            _skips = 0
            winner = rps_winner(kid_move, iris_move)
            if winner == "kid":
                d["kid"] = d.get("kid", 0) + 1
            elif winner == "iris":
                d["iris"] = d.get("iris", 0) + 1
            # match_over lines always ask "Rematch?" regardless of auto_continue:
            # there the turn genuinely does go back to the player.
            reply, emotion, match_over = rps_result_line(
                kid_move, iris_move, winner, d.get("kid", 0), d.get("iris", 0),
                state.kids_mode, auto_continue=_more)
            if match_over:
                d["kid"] = 0; d["iris"] = 0
        if _round == 1:
            try: bench_stages["play_start_ms"] = round((time.monotonic() - t_mono0) * 1000)
            except Exception: pass
        _interrupted = _speak_game_line(reply, emotion, teensy, leds, pa, mic)
        if match_over or _interrupted or not _more or _skips >= 2:
            print(f"[GAME] RPS match end r{_round}: over={match_over} "
                  f"interrupted={_interrupted} skips={_skips}", flush=True)
            break
    return reply, emotion, _interrupted, True


# ── Follow-up ─────────────────────────────────────────────────────────────────
# implies_followup() moved to core/speech_gates.py (S192 AUD-7 test-suite
# session) -- pure predicate, no behavior change, imported below.

def record_followup(mic, pa, leds, timeout=None, play_beep=True, asked_question=False,
                    teensy=None):
    # S194 Rung3: when IRIS just asked the user a question (reply ended in '?'),
    # give a longer speech-start window and a longer endpoint so a natural
    # think-then-answer pause doesn't kill the exchange (adult only; kids keeps
    # its own longer windows). Non-question invites keep the snappy 2.0/0.8.
    # Endpoint tunables read off the live core.config module, not this module's
    # `from core.config import *` binding, which froze them at import so a WebUI
    # RELOAD_CONFIG never reached them (RD-047 follow-up; same fix as
    # hardware/audio_io.record_command).
    import core.config as _cfg
    if timeout is None:
        if state.kids_mode:
            timeout = _cfg.KIDS_FOLLOWUP_TIMEOUT
        else:
            timeout = _cfg.FOLLOWUP_TIMEOUT_QUESTION if asked_question else _cfg.FOLLOWUP_TIMEOUT
    leds.show_followup()
    # S199 T4 (contract rung 7): kids get a DISTINCT rising "your turn" cue +
    # CURIOUS face instead of the generic double-beep, so a child sees and
    # hears the handback. Adults keep the double-beep unchanged.
    if state.kids_mode and _cfg.KIDS_FOLLOWUP_CUE:
        if play_beep:
            try:
                play_your_turn_cue(pa)
                print("[FLWP] your-turn cue", flush=True)
            except Exception as _fe:
                print(f"[FLWP] your-turn cue failed: {_fe} -- double beep", flush=True)
                play_double_beep(pa)
        if teensy is not None:
            try:
                emit_emotion(teensy, leds, "CURIOUS")
            except Exception:
                pass
    elif play_beep:
        play_double_beep(pa)
    frames = []; silence = 0; speech_detected = False
    if state.kids_mode:
        sil_secs = _cfg.KIDS_SILENCE_SECS
    else:
        sil_secs = _cfg.SILENCE_SECS_FOLLOWUP if asked_question else _cfg.SILENCE_SECS
    sil_floor = _cfg.KIDS_SILENCE_RMS    if state.kids_mode else _cfg.SILENCE_RMS
    rec_secs  = _cfg.KIDS_RECORD_SECONDS if state.kids_mode else _cfg.RECORD_SECONDS
    sil_limit = int(SAMPLE_RATE / CHUNK * sil_secs)
    max_chunks = int(SAMPLE_RATE / CHUNK * (timeout + rec_secs))
    timeout_chunks = int(SAMPLE_RATE / CHUNK * timeout); chunks_read = 0
    # S199 T6: same ambient-relative floors as record_command. The fixed floor
    # (kids 150) sat below room tone, so "speech" started on the first ambient
    # chunk and the trailing close never fired -- follow-up windows ran to cap.
    rms_hist = []
    mic.start_stream()
    for _ in range(max_chunks):
        f = mic.read(CHUNK, exception_on_overflow=False); chunks_read += 1
        rms = float(np.sqrt(np.mean(np.frombuffer(f, dtype=np.int16).astype(np.float32)**2)))
        # Baseline frozen at speech onset -- same clipping bug + fix as
        # record_command (see hardware/audio_io.py, S199 fake-mic sim).
        if not speech_detected:
            rms_hist.append(rms)
        baseline = float(np.percentile(rms_hist, 20)) if len(rms_hist) >= 3 else None
        if not speech_detected:
            if baseline is not None and rms > max(sil_floor, baseline * _cfg.ENDPOINT_SPEECH_MULT):
                speech_detected = True; frames.append(f)
            elif chunks_read >= timeout_chunks: mic.stop_stream(); return None
        else:
            frames.append(f)
            sil_rms = max(sil_floor, (baseline or 0.0) * _cfg.ENDPOINT_SILENCE_MULT)
            # S201 (A5): leaky close, same as record_command -- intermittent room
            # noise no longer resets the trailing-silence counter to zero.
            silence = silence + 1 if rms < sil_rms else max(0, silence - _cfg.ENDPOINT_SILENCE_DECAY)
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
# S212c: X_GAIN / Y_GAIN / X_BIAS shape the gaze mapping (targetN = rawN * gain + bias);
# gain sign = direction, magnitude = range. X_GAIN=+1.0 is the SEN0626 convention (the
# fitted sensor). The Person Sensor uses the OPPOSITE sign, so a rollback to
# USE_PERSON_SENSOR_I2C must set X_GAIN=-1.0 here and in ps_config.json or the eyes
# track mirrored. Keep this dict in sync with _PS_CFG_DEFAULTS in iris_web.py.
PS_CFG_DEFAULTS = {"CONF": 60, "FACING": 1, "LOST_MS": 5000, "Y_BIAS": 0.0, "LED": 0,
                   "X_GAIN": 1.0, "Y_GAIN": 1.0, "X_BIAS": 0.0}


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
    # Live config module handle: RELOAD_CONFIG rebinds core.config's globals, so
    # anything WebUI-tunable must be read through this, not a frozen `from ...
    # import X` binding (RD-047).
    import core.config as _cfg
    # kids_mode always starts False; clear any flag left by a crash (RD-047).
    _sync_kids_mode_flag()
    # S202: a quiet break never survives a restart (break_until is in-memory);
    # clear any orphaned flag so /api/health doesn't show a phantom break.
    try: os.remove("/tmp/iris_break_mode")
    except FileNotFoundError: pass
    except Exception: pass
    state.break_until = 0.0
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
    _tts_engine = "Kokoro" if _cfg.KOKORO_ENABLED else "Piper"
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
    # S188: warm quips in the background (gated + retried) so a sleeping
    # GandalfAI can never block the wakeword loop at boot.
    threading.Thread(target=_bg_quip_warm, name="quip-warm", daemon=True).start()
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

    # ── Sleep-state reconciliation (clock-authoritative, S193) ────────────────
    # state.eyes_sleeping is in-memory and defaults False; the /tmp flag lives in
    # RAM and survives a service restart (only a full reboot clears it). The wall
    # clock is the source of truth at startup:
    #   * inside the sleep window  -> it is bedtime, (re-)assert sleep (idempotent).
    #   * flag present but daytime -> a wake was MISSED (a restart carried a stale
    #     flag forward). Wake and clear the flag; do NOT re-sleep through the day.
    # (S193 Bug B: the old code trusted the flag OR the window and could pin IRIS
    #  asleep all day until the next 08:00 wake cron whenever a stale flag existed
    #  at a daytime restart.)
    if in_sleep_window():
        _do_sleep(teensy, leds)
        print("[SLEEP] Startup reconcile: sleep window active -- sleep re-asserted", flush=True)
    elif os.path.exists('/tmp/iris_sleep_mode'):
        _do_wake(teensy, leds)
        print("[WAKE] Startup reconcile: stale sleep flag cleared (daytime) -- woke", flush=True)

    try:
        while True:
            # S192m AUD-12/B4: stamp liveness at the top of every loop iteration.
            _write_heartbeat("processing")

            # Restart OWW process if it has died
            if oww_proc is None or oww_proc.poll() is not None:
                print("[WARN] openwakeword process not running -- attempting restart", flush=True)
                global _heartbeat_oww_restarts
                _heartbeat_oww_restarts += 1
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

            # S192m AUD-12/B4: stamp just before the blocking wait -- this call
            # can block indefinitely between wakes, so age-since-'waiting' is
            # the normal/expected state, not a stall signal on its own.
            _write_heartbeat("waiting")
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
                # S240f: this branch used to reconnect ONLY the OWW socket, so a
                # mic stream that was stopped or closed made every retry raise
                # the same error forever and IRIS went permanently deaf until a
                # service restart. Observed live 2026-07-25 after a quiet break:
                # hundreds of "[Errno -9988] Stream closed" in a tight loop while
                # the red confirm LED kept animating (the anim thread survives; it
                # is the main loop that is stuck). Recover the mic here too:
                # start_stream() first (cheap, fixes a merely-stopped stream), and
                # if the stream is genuinely closed, reopen it from scratch. Any
                # failure just falls through to the next retry rather than killing
                # the loop, so this can only ever improve on spinning forever.
                mic = _recover_mic(mic, pa)
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
            _sleep_break = False   # S194: True when a double-wake breaks through the sleep window

            if ptt_mode: print("\n[PTT]  Button pressed", flush=True); leds.show_ptt()
            else: print("\n[WAKE] Wake word detected", flush=True); leds.show_wake()

            # Sleep mode check — quip is pre-cached PCM, no Gandalf needed.
            # S194: a lone wakeword plays a quip and re-sleeps (nights stay quiet);
            # a SECOND wakeword within SLEEP_DOUBLE_WAKE_WINDOW_S breaks through to a
            # full listen-and-respond turn on demand. The end-of-loop sleep-window
            # check re-sleeps IRIS automatically after the break-through turn.
            if os.path.exists('/tmp/iris_sleep_mode'):
                # S203 (operator directive) -- a lone wakeword during sleep is a REAL
                # wake, awake-hours behavior. The S194/S197 double-tap gate (a first
                # wakeword only quipped + re-slept, requiring a SECOND within
                # SLEEP_DOUBLE_WAKE_WINDOW_S) made her feel offline half the time and
                # never sent Wake-on-LAN, so even the double-tap started cold. Removed.
                # Now: wake the display, play an immediate wake/filler quip (a gapless
                # audible ack over the wake->boot->record gap), then fall through to
                # ensure_gandalf_up() (sends WoL + speaks a "waking my brain" boot
                # filler while GandalfAI cold-boots) and a full record->STT->LLM turn.
                # The end-of-loop in_sleep_window() check re-sleeps her after the
                # interaction ends, so nights stay quiet BETWEEN interactions without
                # gating the interaction itself. (Now unused: _sleep_wake_state,
                # _play_sleep_window_quip, SLEEP_DOUBLE_WAKE_WINDOW_S, sleep_window bank.)
                _do_wake(teensy, leds)
                _play_wake_quip(time.localtime().tm_hour, pa, teensy, leds)
                _sleep_break = True   # skip the snark RPQR cascade; go straight to WoL+listen

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
            # S221 (plan A): same relief for conversation -- a wakeword while a
            # conversation is still live (IRIS spoke within CONVO_REENTRY_GRACE_S)
            # is a re-entry, not a fresh greeting: no quip cascade, short earcon,
            # straight to listen. Adult only -- kids keep their S199 kid-register
            # wake ack (deliberate). 0 = off (pre-S221 behavior).
            _convo_recent = (not state.kids_mode
                             and _cfg.CONVO_REENTRY_GRACE_S > 0
                             and _rpqr_state["t_last_spoke"] > 0
                             and _now_rpqr - _rpqr_state["t_last_spoke"] < _cfg.CONVO_REENTRY_GRACE_S)
            if _sleep_break:
                print("[RPQR] Suppressed (sleep break-through -- going straight to listen)", flush=True)
            elif _game_recent:
                print("[RPQR] Suppressed (camera game active/grace)", flush=True)
            elif state.kids_mode:
                # S199 T2: kids mode skips the snark cascade entirely -- first_of_day
                # ("Finally."), double_tap ("Still watching."), post_speech scolds,
                # top_of_hour. A kid gets the kid-register wake ack every time
                # (_play_wake_quip resolves the kids bank internally).
                _play_wake_quip(_h_rpqr, pa, teensy, leds)
            elif _convo_recent:
                # S221 (plan A): no quip, no LLM -- acknowledge with a short
                # local earcon and fall through to the normal listen path.
                print("[RPQR] Suppressed (conversation grace -- straight to listen)", flush=True)
                try:
                    play_endpoint_cue(pa)
                except Exception:
                    pass
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
            _rec_raw, _ep_reason, _ep_secs = record_command(mic, ptt_mode=ptt_mode, kids_mode=state.kids_mode)
            raw = b"".join(_pre_buf) + _rec_raw
            _t_mono_rec = time.monotonic()
            # RD-047 T2 -- the endpoint-close cue. Fires the INSTANT the recorder
            # endpoints, before the RMS gate and before STT is submitted, so the
            # child gets "message received" ~2s earlier than any pipeline-derived
            # signal could arrive. Local earcon + a thinking face. Never fatal.
            # S199 T6: skipped on a no-speech exit ("got it" would be a lie), and
            # success now logs so the cue is journal-verifiable (it never was).
            if state.kids_mode and _cfg.KIDS_ENDPOINT_CUE and _ep_reason != "nospeech":
                try:
                    play_endpoint_cue(pa)
                    emit_emotion(teensy, leds, "CURIOUS")
                    print(f"[ENDCUE] ok endpoint={_ep_reason} rec_s={_ep_secs:.1f}", flush=True)
                except Exception as _ce:
                    print(f"[ENDCUE] failed: {_ce}", flush=True)
            arr = np.frombuffer(raw, dtype=np.int16).astype(float)
            rms = np.sqrt(np.mean(arr**2))
            # S199 T6: divide by CHANNELS too -- the old label double-counted the
            # stereo stream (printed cap x 2) and hid the broken endpoint for weeks.
            print(f"[REC]  {len(raw)/2/CHANNELS/SAMPLE_RATE:.1f}s  RMS={rms:.0f}  endpoint={_ep_reason}", flush=True)
            _t_rec = time.time()
            try:
                _bench_stages["wake_to_record_start_ms"] = round((_t_mono_rec_start - _t_mono_wake) * 1000)
                _bench_stages["record_duration_ms"] = round((_t_mono_rec - _t_mono_rec_start) * 1000)
                print(f"[BENCH] t={_t_rec:.3f} stage=rec_done dur_rec={_t_rec-_t_wake:.2f} wake_to_rec_start_ms={_bench_stages['wake_to_record_start_ms']} record_duration_ms={_bench_stages['record_duration_ms']} rms={rms:.0f} endpoint={_ep_reason}", flush=True)
            except Exception:
                print(f"[BENCH] t={_t_rec:.3f} stage=rec_done dur_rec={_t_rec-_t_wake:.2f} rms={rms:.0f}", flush=True)

            # ── RMS gate + Whisper hallucination filter ────────────────────────
            # F2 (MAD): use the kids RMS floor in kids mode. record_command already
            # captures kids audio down to KIDS_SILENCE_RMS (150 < adult 300), so
            # gating that same audio at the adult 300 dropped a soft-spoken kid into
            # the re-ask loop with no way through. Read live off core.config.
            _rms_gate = _cfg.KIDS_SILENCE_RMS if state.kids_mode else _cfg.SILENCE_RMS
            if rms < _rms_gate:
                print(f"[REC]  Below RMS gate ({rms:.0f} < {_rms_gate}), ignoring", flush=True)
                if state.kids_mode:      # RD-047 site 1: never drop a kid's turn silently
                    _speak_kids_reask(pa, teensy, leds)
                show_idle_for_mode(leds); continue

            leds.show_thinking(); print("[STT]  Transcribing...", flush=True)
            try: text = transcribe(raw)
            except Exception as e:
                print(f"[ERR]  STT: {e}", flush=True)
                leds.show_error()
                # S194 Rung6: speak the failure; keep the 1s LED hold only if no clip.
                if not speak_error("STT_FAIL", kids=state.kids_mode):
                    time.sleep(1)
                show_idle_for_mode(leds); continue

            if not text:
                print("[STT]  Empty transcript", flush=True)
                if state.kids_mode:      # RD-047 site 2
                    _speak_kids_reask(pa, teensy, leds)
                show_idle_for_mode(leds); continue
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
            # "stopwatch", "quietly", "cancelled", etc. (core.speech_gates.phrase_matches)
            if phrase_matches(_text_norm, STOP_PHRASES):
                print(f"[STOP] Main-loop STOP phrase: '{text}'", flush=True)
                _stop_playback.set()
                emit_emotion(teensy, leds, "NEUTRAL")
                show_idle_for_mode(leds)
                print("[INFO] Ready.", flush=True)
                continue

            # Whisper hallucination gate: shared with the follow-up loop via
            # core.speech_gates (S191 audit AUD-1 -- these had drifted apart into
            # two separately-maintained inline copies; now one definition).
            if is_whisper_hallucination(_text_norm):
                print(f"[STT]  Hallucination filtered: '{text}'", flush=True)
                if state.kids_mode:      # RD-047 site 3
                    _speak_kids_reask(pa, teensy, leds)
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
            # RD-068: park this turn's weather clause (or clear it). Set on EVERY
            # turn, not just weather ones -- a non-weather payload has no
            # "weather_clause" key, so this is how the slot gets cleared and a
            # stale clause can never ride into the next question.
            try:
                _weather_current["clause"] = (_result.payload or {}).get("weather_clause", "") or ""
            except Exception as _wpe:
                _weather_current["clause"] = ""
                print(f"[WX]   payload read skipped: {_wpe}", flush=True)
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
                if _result.action == "BREAK":
                    # S202: quiet break. _run_break() blocks this thread for the
                    # whole window (that IS the wakeword-disable mechanism), then
                    # runs the proactive wake + resume-confirm before returning.
                    print("[BREAK] Quiet-break command received", flush=True)
                    _run_break(pa, teensy, leds, mic)
                    print("[INFO] Ready.", flush=True); continue
                if _result.action == "SLEEP":
                    _do_sleep(teensy, leds)
                    if _result.response:
                        _speak_simple(_result.response, 0, ROUTE_REFLEX, "reflex sleep",
                                      teensy, leds, pa, mic, _bench_stages, _t_mono_wake,
                                      _bench_transcript, _gandalf_was_cold)
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
                        _speak_simple(kids_reply, 0, ROUTE_COMMAND, "mode switch",
                                      teensy, leds, pa, mic, _bench_stages, _t_mono_wake,
                                      _bench_transcript, _gandalf_was_cold)
                    emit_emotion(teensy, leds, "NEUTRAL"); show_idle_for_mode(leds)
                    print("[INFO] Ready.", flush=True); continue
                else:
                    # Volume commands
                    vol_reply = handle_volume_command(text)
                    if vol_reply is not None:
                        print(f"[VOL]  {vol_reply}", flush=True)
                        _speak_simple(vol_reply, 0, ROUTE_COMMAND, "vol",
                                      teensy, leds, pa, mic, _bench_stages, _t_mono_wake,
                                      _bench_transcript, _gandalf_was_cold)
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
                                # RD-060 PROVENANCE. This reply was produced with a
                                # real captured frame in hand. Recording it is what
                                # makes the inverse checkable later: a perception
                                # claim that is NOT in this set was made with no
                                # camera evidence at all, which is the S228 pyjama
                                # sighting exactly. Provenance, not pattern matching.
                                _vision_replies.add(reply)
                                print(f"[VIS]  '{reply}'", flush=True)
                            except Exception as e:
                                reply = "I had trouble processing the image."
                                print(f"[ERR]  Vision: {e}", flush=True)
                        _speak_simple(reply, len(reply), ROUTE_UTILITY, "vision",
                                      teensy, leds, pa, mic, _bench_stages, _t_mono_wake,
                                      _bench_transcript, _gandalf_was_cold)
                        emit_emotion(teensy, leds, "NEUTRAL"); show_idle_for_mode(leds)
                        print("[INFO] Ready.", flush=True); continue
                elif _result.response is not None:
                    print(f"[UTIL] {_result.action}: {_result.response}", flush=True)
                    _speak_simple(_result.response, len(_result.response), ROUTE_UTILITY, "utility",
                                  teensy, leds, pa, mic, _bench_stages, _t_mono_wake,
                                  _bench_transcript, _gandalf_was_cold)
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
                        _speak_simple(_result.response, 0, ROUTE_AMBIGUOUS, "ambiguous sleep",
                                      teensy, leds, pa, mic, _bench_stages, _t_mono_wake,
                                      _bench_transcript, _gandalf_was_cold)
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
                if _cam_game == "RPS":
                    # S210: RPS rounds are self-contained (countdown -> capture
                    # -> classify -> code-judged result); the follow-up loop
                    # only carries the between-rounds "again?" exchange.
                    _cam_game_state["data"] = {}
                    reply, _current_emotion, _interrupted, _ok = _play_rps_round(
                        text, True, teensy, leds, pa, mic, _bench_stages, _t_mono_wake)
                else:
                    reply, _current_emotion, _interrupted, _ok = _play_camera_game(
                        _cam_game, text, teensy, leds, pa, mic, _bench_stages, _t_mono_wake)
                if not _ok:
                    leds.show_error()
                    # S194 Rung6 (MAD): a camera-game turn that produced no audio
                    # shouldn't go silent either -- same TTS_FAIL/LLM_DOWN signature.
                    if not speak_error("TTS_FAIL" if (reply or "").strip() else "LLM_DOWN",
                                       kids=state.kids_mode):
                        time.sleep(1)
                    show_idle_for_mode(leds); continue
                _bench_write(_bench_stages, _bench_transcript, len(reply), get_model(), _gandalf_was_cold, ROUTE_UTILITY, _interrupted, emotion=_current_emotion)
                _last_known_emotion = _current_emotion
                # Break 3 (S168): mark the game active so the follow-up loop keeps
                # offering reciprocal turns without the child re-saying the wake word.
                _cam_game_state.update(active=True, game=_cam_game,
                                       turns_remaining=GAME_FOLLOWUP_TURNS, t_ended=0.0)
            else:
                _num_predict = _route_predict(text)
                reply, _current_emotion, _interrupted, _ok = _speak_llm_turn(
                    text, _num_predict, teensy, leds, pa, mic,
                    _bench_stages, _t_mono_wake, gandalf_was_cold=_gandalf_was_cold)
                if not _ok:
                    leds.show_error()
                    # S194 Rung6: never silent. F1 signature: non-empty reply means
                    # the LLM spoke but every TTS engine failed (TTS_FAIL); empty
                    # reply means the stream itself died (LLM_DOWN). Fall back to the
                    # 1s error-LED hold only when no clip is available.
                    if not speak_error("TTS_FAIL" if (reply or "").strip() else "LLM_DOWN",
                                       kids=state.kids_mode):
                        time.sleep(1)
                    show_idle_for_mode(leds); continue
                _bench_write(_bench_stages, _bench_transcript, len(reply), get_model(), _gandalf_was_cold, _bench_route, _interrupted, emotion=_current_emotion)
                _last_known_emotion = _current_emotion
                # S221 (plan E): bookkeeping only -- adult conversational turn.
                if not state.kids_mode:
                    _convo_note_turn()
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
            # S221 (plan E): while the conversation-session machine is enabled,
            # the follow-up window opens after EVERY conversational reply -- the
            # implies_followup() punctuation gate no longer decides whether the
            # floor is held -- and the in-session safety cap replaces
            # FOLLOWUP_MAX_TURNS. Applies in kids mode too (kids keep their own
            # timeouts via record_followup's internal selection). Games are
            # untouched: GAME_FOLLOWUP_TURNS and the game branches are
            # byte-identical. With CONVO_SESSION_ENABLED=False the whole gate
            # reduces exactly to the pre-S221 expression.
            _convo_live = bool(_cfg.CONVO_SESSION_ENABLED)
            _convo_silent = 0
            # S240 (RD-065): the session machine held the floor after EVERY reply,
            # so a turn she was merely told re-opened the mic. 33 windows in the
            # 2026-07-25 morning, several after statements ("remember that today
            # we are getting ready for Matt's wedding"); the operator's word was
            # "pressured reciprocity". The window now needs an invitation from
            # ONE side: her reply asks something (implies_followup, unchanged, so
            # "a question from her still re-opens the mic"), or the user's turn
            # invited a reply. Kids are exempt -- their reciprocity is the S199 T4
            # / S201 A3 feature and the complaint is adult-side.
            _user_invited = user_invites_followup(text)
            while ((implies_followup(reply, in_game=_cam_game_state["active"], kids=state.kids_mode)
                    or _force_followup
                    or (_cam_game_state["active"] and _cam_game_state["turns_remaining"] > 0)
                    or (_convo_live and not _cam_game_state["active"]
                        and (state.kids_mode or _user_invited)))
                   and _followup_turns < (GAME_FOLLOWUP_TURNS if _cam_game_state["active"]
                                          else (_cfg.CONVO_SESSION_MAX_TURNS if _convo_live else FOLLOWUP_MAX_TURNS))
                   and not _interrupted):
                _max_fu = (GAME_FOLLOWUP_TURNS if _cam_game_state["active"]
                           else (_cfg.CONVO_SESSION_MAX_TURNS if _convo_live else FOLLOWUP_MAX_TURNS))
                print(f"[FLWP] Follow-up turn {_followup_turns+1}/{_max_fu}...", flush=True)
                _followup_turns += 1
                _force_followup = False
                if _cam_game_state["active"] and _cam_game_state["turns_remaining"] > 0:
                    _cam_game_state["turns_remaining"] -= 1
                # Break 5 (S168): no double-beep between game exchanges (the clue
                # already invites the guess); keep it for normal follow-ups.
                # S221 (plan E): in-session adult non-game window -- non-question
                # replies get CONVO_SESSION_WINDOW_S instead of the terse
                # FOLLOWUP_TIMEOUT; question replies keep the longer question
                # wait. None preserves record_followup's internal selection
                # (kids mode always None so KIDS_FOLLOWUP_TIMEOUT rules).
                _fu_timeout = None
                if _convo_live and not state.kids_mode and not _cam_game_state["active"]:
                    # max(): a question must never get LESS patience than a
                    # statement (default Q=6.0 sits under the 7.0 window).
                    _fu_timeout = (max(_cfg.FOLLOWUP_TIMEOUT_QUESTION, _cfg.CONVO_SESSION_WINDOW_S)
                                   if reply.strip().endswith('?')
                                   else _cfg.CONVO_SESSION_WINDOW_S)
                # S240 (RD-065): the double-beep IS the pressure signal, so in an
                # adult session it fires only when SHE asked something. A window
                # the USER invited still opens, just quietly -- the cyan
                # show_followup() LED still marks the handback. Kids (S199 T4
                # rising cue), games, and the session-OFF path are untouched.
                _fu_beep = not _cam_game_state["active"]
                if (_fu_beep and _convo_live and not state.kids_mode
                        and not reply.strip().endswith('?')):
                    _fu_beep = False
                followup_audio = record_followup(mic, pa, leds,
                                                 timeout=_fu_timeout,
                                                 play_beep=_fu_beep,
                                                 asked_question=reply.strip().endswith('?'),
                                                 teensy=teensy)
                # S221 (plan E): in-session, one silent window gets a second
                # chance; two in a row wind the session down with a spoken
                # close instead of a silent die. Games and the disabled state
                # keep today's immediate break.
                if followup_audio is None:
                    if _convo_live and not _cam_game_state["active"]:
                        _convo_silent += 1
                        if _convo_silent >= 2:
                            print("[FLWP] No response (silent window 2/2) -- winding down", flush=True)
                            _speak_convo_windup(teensy, leds, pa, mic, state.kids_mode)
                            break
                        print("[FLWP] Silent window 1/2 -- one more chance", flush=True)
                        continue
                    print("[FLWP] No response", flush=True); break
                rms = np.sqrt(np.mean(np.frombuffer(followup_audio, dtype=np.int16).astype(np.float32)**2))
                if rms < 100:
                    if _convo_live and not _cam_game_state["active"]:
                        _convo_silent += 1
                        if _convo_silent >= 2:
                            print("[FLWP] Silent (silent window 2/2) -- winding down", flush=True)
                            _speak_convo_windup(teensy, leds, pa, mic, state.kids_mode)
                            break
                        print("[FLWP] Silent window 1/2 -- one more chance", flush=True)
                        continue
                    print("[FLWP] Silent", flush=True); break
                _convo_silent = 0   # S221: any real speech resets the silent-window count
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
                if _text_norm in WHISPER_HALLUCINATIONS:
                    print(f"[FLWP] Hallucination filtered: '{text}'", flush=True); break
                # Gate: URL/spam hallucination patterns (shared with the main-loop
                # gate's WHISPER_HALLUCINATION_PATTERNS via core.speech_gates --
                # S191 audit AUD-1, was a separately drifting inline tuple here).
                if any(p in _text_norm for p in WHISPER_HALLUCINATION_PATTERNS):
                    print(f"[FLWP] Hallucination filtered: '{text}'", flush=True); break
                # S240 (RD-065): reflex intents on FOLLOW-UP turns. The S202 break
                # and the voice sleep/wake commands were matched by the router on
                # wakeword-initiated turns only, and S221's session machine keeps
                # the mic in follow-up mode after every reply -- so the reflexes
                # were unreachable from inside a conversation, which is exactly
                # when a person wants them. Journal 2026-07-25 10:02:24: "Take a
                # break." arrived here, skipped the router, and came back as chat
                # ("just say the word"). The SAME router instance decides, so
                # there is one matcher, not two. STOP and the polite dismissals
                # keep their own gates below, byte-identical.
                _fu_intent = router.classify(text, state)
                if _fu_intent.route == ROUTE_REFLEX and _fu_intent.action in ("BREAK", "SLEEP", "WAKE"):
                    print(f"[ROUTE] {_fu_intent.route}/{_fu_intent.action} "
                          f"conf={_fu_intent.confidence} (follow-up)", flush=True)
                    if _fu_intent.action == "BREAK":
                        # Blocks this thread for the whole quiet window, same as
                        # the main-loop branch -- that IS the wakeword disable.
                        print("[BREAK] Quiet-break command received", flush=True)
                        _run_break(pa, teensy, leds, mic)
                    elif _fu_intent.action == "SLEEP":
                        _do_sleep(teensy, leds)
                        if _fu_intent.response:
                            _speak_simple(_fu_intent.response, 0, ROUTE_REFLEX, "reflex sleep",
                                          teensy, leds, pa, mic, _fu_stages, _t_mono_fu0,
                                          text, _gandalf_was_cold)
                    else:
                        _do_wake(teensy, leds)
                    break
                # Shape of THIS user turn decides whether the window re-opens
                # after the reply to it. Captured here, off the transcript, so a
                # later branch reassigning `text` cannot change the decision.
                _user_invited = user_invites_followup(text)
                # Word-boundary match (exact or phrase+space), same as the main-loop
                # STOP gate ~line 1509 -- was a bare startswith() here, which let
                # "stopwatch"/"cool as ice"/etc. falsely end the follow-up (S191 AUD-1).
                if phrase_matches(_text_norm, STOP_PHRASES):
                    print("[STOP] Stop in follow-up", flush=True); break
                if phrase_matches(_text_norm, FOLLOWUP_DISMISSALS):
                    print("[FLWP] Polite dismissal, ending follow-up", flush=True)
                    # S221 (plan E): in-session, acknowledge the hand-back with
                    # the wind-down line. The STOP break above stays SILENT by
                    # design -- the user said stop, so say nothing.
                    if _convo_live and not _cam_game_state["active"]:
                        _speak_convo_windup(teensy, leds, pa, mic, state.kids_mode)
                    break
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
                elif _cam_game_state["active"] and _cam_game_state["game"] == "RPS":
                    # S210: between-rounds exchange. Negative -> spoken
                    # sign-off; anything else ("yes", "again", "rematch",
                    # "best of three") -> next self-contained round.
                    # is_negative BEFORE is_affirmative ("no more").
                    if is_negative(_text_norm):
                        reply = game_signoff_line(state.kids_mode)
                        _speak_game_line(reply, "HAPPY", teensy, leds, pa, mic,
                                         user_text=text)
                        break
                    reply, emotion, _interrupted, _fu_ok = _play_rps_round(
                        text, False, teensy, leds, pa, mic, _fu_stages, _t_mono_fu0)
                    if not _fu_ok:
                        break
                    _bench_write(_fu_stages, text, len(reply), get_model(),
                                 False, "GAME_FU", _interrupted, emotion=emotion)
                    _last_known_emotion = emotion
                elif (_cam_game_state["active"] and _cam_game_state["game"] == "I_SPY"
                      and _cam_game_state["data"].get("secret")):
                    # S210: I Spy guesses are judged HERE, in code, against the
                    # stored pick -- never by the LLM (which used to improvise
                    # verdicts without knowing the answer). Instant TTS-only
                    # turns; generous matching so kids win.
                    d = _cam_game_state["data"]
                    if d.get("awaiting_replay"):
                        if is_negative(_text_norm):
                            reply = game_signoff_line(state.kids_mode)
                            _speak_game_line(reply, "HAPPY", teensy, leds, pa, mic,
                                             user_text=text)
                            break
                        if not is_affirmative(_text_norm):
                            break   # unrelated speech: let the game end quietly
                        reply, emotion, _interrupted, _fu_ok = _play_camera_game(
                            "I_SPY", text, teensy, leds, pa, mic, _fu_stages, _t_mono_fu0)
                        if not _fu_ok:
                            break
                        _cam_game_state["turns_remaining"] = GAME_FOLLOWUP_TURNS
                        _bench_write(_fu_stages, text, len(reply), get_model(),
                                     False, "GAME_FU", _interrupted, emotion=emotion)
                        _last_known_emotion = emotion
                    else:
                        if is_giveup(_text_norm):
                            reply, emotion = ispy_reveal_line(d["secret"], state.kids_mode), "HAPPY"
                            d["awaiting_replay"] = True
                        elif judge_ispy_guess(text, d["secret"], d["synonyms"]):
                            reply, emotion = ispy_correct_line(d["secret"], state.kids_mode), "SURPRISED"
                            d["awaiting_replay"] = True
                        else:
                            d["clue_idx"] += 1
                            if d["clue_idx"] < len(d["clues"]):
                                reply = ispy_wrong_line(d["clues"][d["clue_idx"]], state.kids_mode)
                                emotion = "CURIOUS"
                            else:
                                reply, emotion = ispy_reveal_line(d["secret"], state.kids_mode), "HAPPY"
                                d["awaiting_replay"] = True
                        print(f"[GAME] I Spy judge: guess={text!r} -> {reply!r}", flush=True)
                        _interrupted = _speak_game_line(reply, emotion, teensy, leds,
                                                        pa, mic, user_text=text)
                        _last_known_emotion = emotion
                elif _cam_game_state["active"] and _cam_game_state["game"] in ("SHOW_ME", "FACE", "DRAW"):
                    # Break 7 (S168): SHOW_ME / FACE follow-ups re-capture the
                    # child's changed frame and re-ask vision, instead of
                    # answering the guess from stale text context only.
                    # S219: DRAW joins them -- the child is still holding the
                    # picture up when they say "no, it's a cow", so the reaction
                    # has to look at it again. Without this the game starts fine
                    # and then goes dead on the very first answer.
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
                    # Break 6 (S168): in-game speech gets terse reactions -- don't
                    # over-allocate tokens. (S210: I_SPY guesses no longer land
                    # here; this path only sees a game whose pick/setup failed.)
                    _followup_predict = (NUM_PREDICT_SHORT if _cam_game_state["active"]
                                         else _route_predict(text))
                    reply, emotion, _interrupted, _fu_ok = _speak_llm_turn(
                        text, _followup_predict, teensy, leds, pa, mic,
                        _fu_stages, _t_mono_fu0, stage_prefix="fu_")
                    if not _fu_ok:
                        break
                    _bench_write(_fu_stages, text, len(reply), get_model(),
                                 False, "FOLLOWUP", _interrupted, emotion=emotion)
                    _last_known_emotion = emotion
                    # S221 (plan E): bookkeeping only -- adult conversational turn.
                    if not state.kids_mode and not _cam_game_state["active"]:
                        _convo_note_turn()
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
