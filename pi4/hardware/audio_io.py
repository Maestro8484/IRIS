"""
hardware/audio_io.py - Audio input/output and volume control
wm8960 HAT, PyAudio, PCM playback with interrupt detection, record, volume.

Key design notes:
- _stop_playback is a module-level Event; import it to call .set() from outside.
- record_command() takes kids_mode as an explicit parameter (not a global).
- play_pcm_speaking() takes a TeensyBridge instance for mouth animation.
- _playback_interrupt_listener() uses adaptive baseline to ignore speaker bleed.
"""

import json
import os
import queue
import re
import subprocess
import threading
import time

import numpy as np
import pyaudio

from core.config import (
    SAMPLE_RATE, CHUNK, CHANNELS,
    RECORD_SECONDS, SILENCE_SECS, SILENCE_RMS,
    KIDS_RECORD_SECONDS, KIDS_SILENCE_SECS, KIDS_SILENCE_RMS,
    VOL_CONTROL, VOL_MIN, VOL_MAX, VOL_STEP,
    LOUD_STOP_THRESHOLD,
)
# STOP_PHRASES / FOLLOWUP_DISMISSALS moved to core/speech_gates.py (S192 AUD-7
# test-suite session) so they're defined once and importable without pyaudio;
# re-exported here unchanged so existing `from hardware.audio_io import
# STOP_PHRASES, FOLLOWUP_DISMISSALS` call sites (assistant.py) keep working.
from core.speech_gates import STOP_PHRASES, FOLLOWUP_DISMISSALS
from hardware.io import button_pressed


# ── Shared stop-playback event (importable by orchestrator) ───────────────────
_stop_playback = threading.Event()

# ── Interrupt detection constants ─────────────────────────────────────────────
# RMS threshold for mid-playback voice interrupt.
# NOTE: raised from 1200 → 4000 because the external amp (5V 3W, 3.5mm headphone path)
# at -5dB DAC bleeds acoustically into the ReSpeaker mics at ~1200-4500 RMS.
# A human voice on top of that bleed reaches 5000-8000, so 4000 still catches
# interrupts while ignoring IRIS's own speaker output.
INTERRUPT_RMS_THRESHOLD = 4000

# LOUD_STOP_THRESHOLD imported from core.config — tune via iris_config.json.
# S88 observed bleed at 9k-18k RMS; raised to 25000. Overridable via iris_config.json.


# ── Barge-in (mid-playback voice command) config + Vosk engine ────────────────
# The playback interrupt listener recognizes short spoken commands OVER IRIS's
# own audio. Default engine is Vosk: offline + grammar-constrained, so it reacts
# in a couple hundred ms and can only ever decode the fixed vocabulary (no
# Whisper round-trip, no mishearing arbitrary speech into "stop"). Falls back to
# Wyoming Whisper if Vosk is unavailable. Enable / engine / grammar are read
# LIVE from iris_config.json at each listener start (WebUI Gestures tab -> Voice
# Barge-in card), so a change applies on the next spoken reply with no restart.
_BARGEIN_CONFIG_PATH = "/home/pi/iris_config.json"
_VOSK_PKG_PATH       = "/home/pi/vosk_pkg"
_VOSK_MODEL_PATH     = "/home/pi/vosk-model-small-en-us-0.15"

# phrase -> action (STOP | VOL+ | VOL-). Every phrase must be common English
# (present in the Vosk small-model lexicon). Mirrors iris_web.py's default.
_DEFAULT_BARGEIN_GRAMMAR = {
    "stop": "STOP", "cancel": "STOP", "be quiet": "STOP", "shut up": "STOP",
    "stop talking": "STOP", "pause": "STOP",
    "louder": "VOL+", "volume up": "VOL+",
    "quieter": "VOL-", "volume down": "VOL-",
}

# S199 T3: kids-mode grammar. One logged kids session had 5 STOP cuts + 12 VOL+
# fires in 40 minutes -- kids shouting along decode against the wide adult
# grammar and hard-cut IRIS mid-reply / ratchet the volume. Kids keep a
# deliberate stop; volume phrases are removed. Override via
# BARGEIN_GRAMMAR_KIDS in iris_config.json (same shape as BARGEIN_GRAMMAR).
_DEFAULT_BARGEIN_GRAMMAR_KIDS = {"stop": "STOP", "be quiet": "STOP"}
_KIDS_MODE_FLAG = "/tmp/iris_kids_mode"   # maintained by assistant._set_kids_mode

_vosk_model       = None
_vosk_model_lock  = threading.Lock()
_vosk_unavailable = False


def _get_vosk_model():
    """Lazy-load the Vosk model once (~50 MB resident, ~1.5 s load) and reuse it
    across every listener. Returns None and latches unavailable on any failure so
    the listener falls back to Whisper."""
    global _vosk_model, _vosk_unavailable
    if _vosk_unavailable or _vosk_model is not None:
        return _vosk_model
    with _vosk_model_lock:
        if _vosk_model is None and not _vosk_unavailable:
            try:
                import sys as _sys
                if _VOSK_PKG_PATH not in _sys.path:
                    _sys.path.insert(0, _VOSK_PKG_PATH)
                from vosk import Model, SetLogLevel
                SetLogLevel(-1)          # silence Vosk's per-utterance LOG spam
                _vosk_model = Model(_VOSK_MODEL_PATH)
                print("[INT]  Vosk model loaded", flush=True)
            except Exception as e:
                print(f"[INT]  Vosk unavailable ({e}) -- Whisper fallback", flush=True)
                _vosk_unavailable = True
    return _vosk_model


def _load_bargein_config():
    """(enabled, engine, grammar, kids, detect_mult, guard_s) read live from
    iris_config.json each listener start. Missing / malformed -> safe defaults
    (enabled, vosk, default grammar). S199 T3: kids mode is read from the /tmp
    flag file assistant.py maintains -- kids get a tighter grammar, a higher
    detect multiplier, and a short guard window against talk-along cuts. All
    values overridable in iris_config.json, contract in
    docs/S199_kids_tempo_contract.md."""
    try:
        with open(_BARGEIN_CONFIG_PATH) as _f:
            _cfg = json.load(_f)
    except Exception:
        _cfg = {}
    enabled = bool(_cfg.get("BARGEIN_ENABLED", True))
    engine  = str(_cfg.get("BARGEIN_ENGINE", "vosk")).lower()
    kids    = os.path.exists(_KIDS_MODE_FLAG)
    grammar = _cfg.get("BARGEIN_GRAMMAR_KIDS" if kids else "BARGEIN_GRAMMAR")
    if not isinstance(grammar, dict) or not grammar:
        grammar = dict(_DEFAULT_BARGEIN_GRAMMAR_KIDS if kids else _DEFAULT_BARGEIN_GRAMMAR)
    if kids:
        detect_mult = float(_cfg.get("KIDS_BARGEIN_DETECT_MULT", 2.0))
        guard_s     = float(_cfg.get("KIDS_BARGEIN_GUARD_MS", 800)) / 1000.0
    else:
        detect_mult = float(_cfg.get("BARGEIN_DETECT_MULT", 1.5))
        guard_s     = 0.0
    return enabled, engine, grammar, kids, detect_mult, guard_s


def _bargein_warm():
    """Background model warm so the first barge-in doesn't eat the load latency
    (mirrors assistant.py's _bg_quip_warm). Non-fatal on any failure."""
    try:
        enabled, engine, *_ = _load_bargein_config()
        if enabled and engine == "vosk":
            _get_vosk_model()
    except Exception:
        pass


threading.Thread(target=_bargein_warm, daemon=True).start()


# ── Device discovery ──────────────────────────────────────────────────────────

def _find_mic_device_index() -> int | None:
    """Find wm8960 capture device by name so index shifts on reboot don't break us."""
    try:
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d['maxInputChannels'] > 0 and 'capture' in d['name'].lower():
                p.terminate()
                print(f"[MIC]  Auto-selected device {i}: {d['name']}", flush=True)
                return i
        p.terminate()
    except Exception as e:
        print(f"[MIC]  Auto-detect failed: {e}", flush=True)
    print("[MIC]  Using system default input device", flush=True)
    return None


def _find_wm8960_card() -> int:
    """Return ALSA card number for wm8960 HAT (default 1 if not found)."""
    try:
        out = subprocess.check_output(['aplay', '-l'], text=True)
        for line in out.splitlines():
            if 'wm8960' in line.lower():
                return int(line.split()[1].rstrip(':'))
    except Exception:
        pass
    return 1


# ── Volume control ────────────────────────────────────────────────────────────

def get_volume() -> int:
    try:
        out = subprocess.check_output(
            ["amixer", "-c", str(_find_wm8960_card()), "sget", VOL_CONTROL],
            text=True, timeout=5)
        for line in out.splitlines():
            if "Playback" in line and "[" in line:
                m = re.search(r"Playback (\d+)", line)
                if m:
                    return int(m.group(1))
    except Exception as e:
        print(f"[VOL]  get_volume failed: {e} -- returning fallback 110", flush=True)
    return 110


def set_volume(level: int, allow_zero: bool = False) -> int:
    # allow_zero bypasses the VOL_MIN floor for the gesture MUTE toggle;
    # voice commands keep the floor so "volume down" can never silence IRIS.
    # VOL_MAX read live (RD-047 follow-up): the module-level import froze it, so
    # a WebUI VOL_MAX change needed a service restart to take effect.
    import core.config as _cfg
    VOL_MAX = _cfg.VOL_MAX
    floor = 0 if allow_zero else VOL_MIN
    level = max(floor, min(VOL_MAX, level))
    subprocess.run(
        ["amixer", "-c", str(_find_wm8960_card()), "sset", VOL_CONTROL, str(level)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return level


# S240c: percent <-> register conversion across the AUDIBLE band only.
# The wm8960 Speaker control is 1 dB per step (dB = register - 121), so treating
# it as a linear percentage is a category error: the old
# `int(pct / 100 * VOL_MAX)` put "set volume to 50 percent" at register 63,
# which is -58 dB and completely silent. Mapping onto
# [VOL_USABLE_MIN, VOL_MAX] instead makes 50 percent land near -14 dB, an actual
# listening level, and makes the percentage IRIS speaks back mean something.
def _pct_to_reg(pct: int) -> int:
    import core.config as _cfg
    lo, hi = _cfg.VOL_USABLE_MIN, _cfg.VOL_MAX
    return lo + int(round(max(0, min(100, pct)) / 100.0 * (hi - lo)))


def _reg_to_pct(reg: int) -> int:
    import core.config as _cfg
    lo, hi = _cfg.VOL_USABLE_MIN, _cfg.VOL_MAX
    if hi <= lo:
        return 0
    return max(0, min(100, int(round((reg - lo) / float(hi - lo) * 100))))


def handle_volume_command(text: str) -> str | None:
    """Handle voice volume commands. Returns response string or None if not a volume command."""
    import core.config as _cfg
    VOL_MAX = _cfg.VOL_MAX          # live, not the frozen import (RD-047 follow-up)
    VOL_STEP = _cfg.VOL_STEP        # S240c: live, same pattern as VOL_MAX above
    t = text.lower().strip().rstrip(".!?")
    pct_match = re.search(r'(\d+)\s*(?:percent|%)', t)
    if pct_match and 'volume' in t:
        target = max(VOL_MIN, min(VOL_MAX, _pct_to_reg(int(pct_match.group(1)))))
        set_volume(target)
        return f"Volume set to {_reg_to_pct(target)} percent."
    if any(p in t for p in ("all the way up", "max volume", "volume max",
                             "full volume", "maximum volume", "as loud")):
        set_volume(VOL_MAX); return "Volume set to maximum."
    if any(p in t for p in ("all the way down", "volume low", "minimum volume",
                             "volume minimum", "as quiet")):
        # S240c: the quietest STILL-AUDIBLE setting, not VOL_MIN. VOL_MIN is
        # -61 dB, so the old behavior silenced her outright -- which contradicted
        # set_volume()'s own comment that a voice command can never do that.
        set_volume(_cfg.VOL_USABLE_MIN); return "Volume set to minimum."
    if any(p in t for p in ("volume up", "louder", "turn it up", "increase volume",
                             "turn up", "raise volume", "higher volume", "more volume")):
        return f"Volume increased to {_reg_to_pct(set_volume(get_volume() + VOL_STEP))} percent."
    if any(p in t for p in ("volume down", "quieter", "turn it down", "decrease volume",
                             "lower volume", "turn down", "reduce volume",
                             "less volume", "softer", "too loud")):
        return f"Volume decreased to {_reg_to_pct(set_volume(get_volume() - VOL_STEP))} percent."
    if any(p in t for p in ("what's the volume", "whats the volume", "current volume",
                             "volume level", "how loud", "what volume")):
        return f"Volume is at {_reg_to_pct(get_volume())} percent."
    if 'volume' in set(t.split()) and len(t.split()) <= 6:
        return f"Volume is at {_reg_to_pct(get_volume())} percent."
    return None


# ── Playback interrupt listener ───────────────────────────────────────────────

def _playback_interrupt_listener(pa_ref, stop_event, interrupted_event, ref_tap=None):
    """
    Background thread: opens a separate mic stream during playback.
    Triggers interrupted_event if voice matches a STOP_PHRASES phrase via STT.

    ref_tap (S214 A8): optional hardware.aec.RefTap passed by play_pcm_stream
    when BARGEIN_AEC_ENABLED. When present and the canceller converges, the
    phrase-recognizer feed switches to the echo-cancelled signal and a presence
    tier can yield a STOP on sustained conversational-volume speech. When None
    (default, all other callers) this function behaves exactly as before A8.

    Phase 1: measures the speaker-bleed baseline from AUDIBLE playback only,
             skipping leading/gap silence so it reflects IRIS's real speaker
             bleed and not a startup gap (S203 -- the frozen baseline used to
             catch silence, e.g. RMS 1108, dropping detect_threshold far below
             her true bleed so her own voice self-decoded a grammar word and
             she cut herself off mid-reply).
    Phase 2: detects voice above detect_threshold; collects the utterance; and
             verifies via Vosk grammar (Wyoming Whisper fallback). Fires the
             interrupt only on a grammar / STOP_PHRASES match.
    """
    # Live config handle, resolved once per playback (not per audio chunk).
    # The module-level `from core.config import LOUD_STOP_THRESHOLD` froze the
    # value at import, so a WebUI tune never reached it (RD-047 follow-up).
    import core.config as _cfg
    # S199 T3: config (incl. kids grammar / detect multiplier / guard window)
    # resolved up front so the detect threshold can use the configured
    # multiplier. Was a hardcoded 1.5 and a post-baseline load.
    (bargein_enabled, bargein_engine, bargein_grammar,
     bargein_kids, _DETECT_MULTIPLIER, _GUARD_S) = _load_bargein_config()
    _COLLECT_CHUNKS    = int(SAMPLE_RATE / CHUNK * 1.5)   # max utterance length
    _SILENCE_CHUNKS    = int(SAMPLE_RATE / CHUNK * 0.30)  # trailing silence ends utterance
    _aec  = None    # S214 A8 state, predeclared so summary/error paths are safe
    _gate = None
    _in_lat = 0.0

    try:
        mon = pa_ref.open(rate=SAMPLE_RATE, channels=CHANNELS,
                          format=pyaudio.paInt16, input=True,
                          frames_per_buffer=CHUNK)

        # S214 A8: optional software echo cancellation. Constructed only when the
        # writer passed a reference tap AND the master flag is on; every failure
        # fails OPEN to the raw pre-A8 listener behavior (docs/S214_A8_design.md §5).
        if ref_tap is not None:
            try:
                from hardware.aec import EchoCanceller, AecGate, load_aec_config
                _acfg = load_aec_config(_BARGEIN_CONFIG_PATH)
                if _acfg["enabled"]:
                    _aec = EchoCanceller(rate=SAMPLE_RATE)
                    try:
                        _in_lat = float(mon.get_input_latency())
                    except Exception:
                        _in_lat = 0.0
                    _gate = AecGate(_acfg, kids=bargein_kids, rate=SAMPLE_RATE, chunk=CHUNK)
                    print(f"[AEC]  active (in_lat={_in_lat*1000:.0f}ms)", flush=True)
            except Exception as _e:
                print(f"[AEC]  unavailable ({_e}) -- raw path", flush=True)
                _aec = None
                _gate = None

        def _aec_process(data, raw_rms):
            # Echo-cancel one mic chunk. Returns (clean_mono_int16, clean_rms) or
            # (None, 0.0). Any error kills AEC for this reply only (fail open).
            nonlocal _aec
            if _aec is None or _gate.dead:
                return None, 0.0
            try:
                mono = np.frombuffer(data, dtype=np.int16)
                if CHANNELS > 1:
                    mono = mono.reshape(-1, CHANNELS).mean(axis=1).astype(np.int16)
                ref = _gate.fetch(ref_tap, len(mono), _in_lat)
                if ref is None:
                    return None, 0.0
                clean = _aec.process(mono, ref)
                clean_rms = float(np.sqrt(np.mean(clean.astype(np.float32) ** 2)))
                _gate.update(raw_rms, clean_rms, float(INTERRUPT_RMS_THRESHOLD))
                return clean, clean_rms
            except Exception as _e:
                print(f"[AEC]  error ({_e}) -- raw path for this reply", flush=True)
                _aec = None
                return None, 0.0

        # Phase 1: measure the speaker-bleed baseline from AUDIBLE playback only.
        # S203 self-cutoff fix. The old fixed-count 0.5s window frequently landed on
        # startup silence or an inter-sentence gap and measured bleed_rms as low as
        # ~1100 (pure silence) or ~5000 (a quiet gap). That collapsed detect_threshold
        # (= bleed_rms x mult) far below her true speaking bleed (~20000-26000 at vol
        # 117), so her own voice flooded the phrase recognizer for the rest of the reply
        # and eventually self-decoded a grammar word ("stop"/"pause"/"louder") -> she cut
        # / re-volumed herself mid-reply. In the logs EVERY false interrupt is preceded by
        # a low baseline; every high baseline (>=15000) is clean. Fix: skip sub-floor
        # (silence/gap) chunks and keep reading -- up to a hard cap so we never block --
        # until we have ~0.8s of her ACTUAL bleed, then take the 90th percentile so the
        # threshold reflects her loud syllables, not a quiet moment. Final sensitivity is
        # the live-tunable BARGEIN_DETECT_MULT (raise it if she ever still self-cuts).
        _BLEED_FLOOR    = float(INTERRUPT_RMS_THRESHOLD)      # >this = her voice, not a gap
        _target_audible = int(SAMPLE_RATE / CHUNK * 0.8)      # ~0.8s of real bleed wanted
        _read_cap       = int(SAMPLE_RATE / CHUNK * 5.0)      # ~5s hard cap; never hangs
        baseline_vals = []   # audible chunks only (her real speaker bleed)
        _all_vals     = []   # everything read, for the silent-reply fallback
        for _ in range(_read_cap):
            if stop_event.is_set() or len(baseline_vals) >= _target_audible:
                break
            try:
                data = mon.read(CHUNK, exception_on_overflow=False)
            except Exception:
                break
            rms = np.sqrt(np.mean(
                np.frombuffer(data, dtype=np.int16).astype(np.float32) ** 2))
            _all_vals.append(rms)
            if rms > _BLEED_FLOOR:
                baseline_vals.append(rms)
            if _aec is not None:
                _aec_process(data, rms)   # S214 A8: adapt during the baseline window (free convergence)

        # Use the audible chunks if we captured a decent sample of her bleed; otherwise
        # she was genuinely quiet/silent this window -> fall back to whatever we saw
        # (a low threshold is then correct because her bleed really is low).
        _src = baseline_vals if len(baseline_vals) >= max(3, _target_audible // 2) else _all_vals
        if _src:
            bleed_rms = float(np.percentile(_src, 90))
            detect_threshold = max(float(INTERRUPT_RMS_THRESHOLD),
                                   bleed_rms * _DETECT_MULTIPLIER)
        else:
            bleed_rms = 0.0
            detect_threshold = float(INTERRUPT_RMS_THRESHOLD)
        print(f"[INT]  Bleed baseline RMS={bleed_rms:.0f}  detect_threshold={detect_threshold:.0f} "
              f"(audible={len(baseline_vals)}/{len(_all_vals)})", flush=True)

        # Adaptive loud-stop floor: tracks highest bleed RMS seen while IRIS is speaking so
        # the instant-interrupt threshold stays above speaker bleed even when the 0.5s baseline
        # window started during an inter-sentence gap (giving a misleadingly low bleed_rms).
        peak_bleed = bleed_rms

        # Phase 2: recognize barge-in commands. Engine/enable/grammar are read
        # live from iris_config.json so a WebUI change applies on this next
        # utterance without a restart. Vosk (grammar-constrained, offline) is
        # default; Whisper (collect + transcribe) is the fallback. The loud-stop
        # instant path below stays active regardless of engine/enable.
        _t_phase2 = time.monotonic()   # S199 T3: kids guard window anchor
        vosk_model = _get_vosk_model() if (bargein_enabled and bargein_engine == "vosk") else None
        use_vosk = vosk_model is not None
        vosk_rec = None
        if use_vosk:
            try:
                from vosk import KaldiRecognizer
                _grammar_list = list(bargein_grammar.keys()) + ["[unk]"]
                vosk_rec = KaldiRecognizer(vosk_model, SAMPLE_RATE, json.dumps(_grammar_list))
                print(f"[INT]  Barge-in: Vosk grammar ({len(bargein_grammar)} phrases, kids={int(bargein_kids)})", flush=True)
            except Exception as e:
                print(f"[INT]  Vosk recognizer init failed ({e}) -- Whisper fallback", flush=True)
                use_vosk = False

        def _do_bargein_action(action, phrase=""):
            # Returns True if the action was executed (False = guard-suppressed).
            # S199 T3: in kids mode, matches inside the guard window right after
            # playback starts are talk-along, not commands -- log and drop.
            if _GUARD_S and (time.monotonic() - _t_phase2) < _GUARD_S:
                print(f"[INT]  Match '{phrase}' suppressed (kids guard {int(_GUARD_S*1000)}ms)", flush=True)
                return False
            if action == "STOP":
                print(f"[INT]  Stop phrase matched: '{phrase}' (kids={int(bargein_kids)}) -- firing interrupt", flush=True)
                interrupted_event.set()
                _stop_playback.set()
                return True
            if action in ("VOL+", "VOL-"):
                try:
                    handle_volume_command("louder" if action == "VOL+" else "quieter")
                    print(f"[INT]  Barge-in volume: {action}", flush=True)
                except Exception as e:
                    print(f"[INT]  Barge-in volume error: {e}", flush=True)
                return True
            return False

        def _match_grammar(text):
            # Returns (action, phrase) or (None, "").
            t = (text or "").strip().lower()
            if not t:
                return None, ""
            # longest phrase first so "stop talking" wins over "stop"
            for phrase in sorted(bargein_grammar, key=len, reverse=True):
                if phrase == t or (" " + phrase + " ") in (" " + t + " "):
                    return bargein_grammar[phrase], phrase
            return None, ""

        # Whisper-path collection state (used only when not use_vosk).
        collect_frames = []
        collecting = False
        silence_count = 0
        stt_pending = threading.Event()
        vosk_idle = 0
        _last_vosk_phrase = None   # S201 (A2): last PARTIAL grammar match, for confirm-on-repeat

        def _verify_stt(frames):
            try:
                import services.stt as _stt
                transcript = _stt.transcribe(b"".join(frames)).lower().strip()
                print(f"[INT]  STT: '{transcript}'", flush=True)
                action, phrase = _match_grammar(transcript)
                if action is None and any(p in transcript for p in STOP_PHRASES):
                    action, phrase = "STOP", transcript   # legacy STOP_PHRASES coverage
                if action:
                    _do_bargein_action(action, phrase)
            except Exception as e:
                print(f"[INT]  STT error: {e}", flush=True)
            finally:
                stt_pending.clear()

        while not stop_event.is_set():
            try:
                data = mon.read(CHUNK, exception_on_overflow=False)
            except Exception:
                break
            rms = np.sqrt(np.mean(
                np.frombuffer(data, dtype=np.int16).astype(np.float32) ** 2))

            # S214 A8: echo-cancel this chunk when AEC is active. On success AND
            # converged (ERLE watchdog), the phrase-recognizer feed below switches
            # to the clean signal + residual gate; loud-stop and peak_bleed stay
            # on the raw signal ALWAYS (a shout must work even through an AEC bug).
            _clean = None
            _clean_rms = 0.0
            if _aec is not None:
                _clean, _clean_rms = _aec_process(data, rms)
            _use_clean = _clean is not None and _gate.converged

            # Update peak bleed from IRIS's speaker voice while not collecting user speech.
            # Any RMS above detect_threshold while idle is IRIS's bleed, not the user.
            if not collecting and rms > detect_threshold:
                peak_bleed = max(peak_bleed, rms)

            # Effective loud-stop floor adapts above observed speaker bleed (1.5x peak).
            # Prevents false instant-interrupt when IRIS's own voice spikes past the fixed
            # LOUD_STOP_THRESHOLD (seen at VOL_MAX=126 where bleed peaks at ~26000-27000).
            effective_loud_stop = max(_cfg.LOUD_STOP_THRESHOLD, peak_bleed * 1.5)
            if rms > effective_loud_stop:
                print(f"[INT]  Loud stop triggered (RMS={rms:.0f} > {effective_loud_stop:.0f}) -- instant interrupt", flush=True)
                interrupted_event.set()
                _stop_playback.set()
                break

            if not bargein_enabled:
                continue   # loud-stop only; no phrase recognition this playback

            # S214 A8: presence tier -- sustained conversational-volume speech over
            # her reply (post-AEC near-end signal) yields a STOP without needing a
            # grammar word. Inert unless AEC is on + converged; disabled in kids
            # mode by default (talk-along protection); kids 800ms guard applies
            # through _do_bargein_action like every other tier.
            if _use_clean and _gate.presence_step(_clean_rms):
                if _do_bargein_action("STOP", "presence"):
                    break

            if use_vosk:
                # Feed only above-bleed audio (the user speaking over IRIS), downmixed
                # to mono, so Vosk never transcribes IRIS's own speaker output. Reset
                # after a short silence so each command is decoded fresh.
                # S214 A8: with AEC converged, the gate tests the echo-cancelled
                # signal against the residual floor (a conversational "stop" passes;
                # her cancelled bleed does not) and Vosk decodes the clean audio.
                # Raw path otherwise -- identical to pre-A8 when the flag is off.
                _feed_hit = (_clean_rms > _gate.feed_floor) if _use_clean \
                            else (rms > detect_threshold)
                if _feed_hit:
                    vosk_idle = 0
                    try:
                        if _use_clean:
                            _samples = _clean
                        else:
                            _samples = np.frombuffer(data, dtype=np.int16)
                            if CHANNELS > 1:
                                _samples = _samples.reshape(-1, CHANNELS).mean(axis=1).astype(np.int16)
                        _is_full = bool(vosk_rec.AcceptWaveform(_samples.tobytes()))
                        if _is_full:
                            _txt = json.loads(vosk_rec.Result()).get("text", "")
                        else:
                            _txt = json.loads(vosk_rec.PartialResult()).get("partial", "")
                        _action, _phrase = _match_grammar(_txt)
                        # S201 (A2): a FULL Vosk result is an end-of-utterance decode
                        # (high confidence) -> fire at once, unchanged. A PARTIAL match
                        # must repeat on two consecutive above-threshold reads before it
                        # fires, so a transient speaker-bleed artifact that momentarily
                        # decodes to a grammar word does not false-cut her. This tightens
                        # DECODE acceptance only; detect_threshold is untouched, so a real
                        # louder-than-bleed "stop" still fires (no Gap-1 desensitization).
                        if _action and (_is_full or _phrase == _last_vosk_phrase):
                            _fired = _do_bargein_action(_action, _phrase)
                            vosk_rec.Reset(); _last_vosk_phrase = None   # fresh decode either way
                            if _fired and _action == "STOP":
                                break
                        elif _action:
                            _last_vosk_phrase = _phrase   # partial first-sighting; await one confirm
                        else:
                            _last_vosk_phrase = None       # match broken; reset the confirm chain
                    except Exception as e:
                        print(f"[INT]  Vosk decode error: {e}", flush=True)
                else:
                    vosk_idle += 1
                    _last_vosk_phrase = None   # S201 (A2): a below-threshold gap breaks the confirm chain
                    if vosk_idle == _SILENCE_CHUNKS:
                        try:
                            vosk_rec.Reset()
                        except Exception:
                            pass
                continue

            # Whisper fallback: collect utterance, verify asynchronously.
            if rms > detect_threshold:
                if not collecting:
                    collecting = True
                    collect_frames = []
                    silence_count = 0
                    print(f"[INT]  Voice detected (RMS={rms:.0f}), collecting...", flush=True)
                collect_frames.append(data)
                silence_count = 0
            elif collecting:
                collect_frames.append(data)
                silence_count += 1
                if silence_count >= _SILENCE_CHUNKS or len(collect_frames) >= _COLLECT_CHUNKS:
                    if not stt_pending.is_set():
                        stt_pending.set()
                        t = threading.Thread(
                            target=_verify_stt, args=(list(collect_frames),), daemon=True)
                        t.start()
                    collecting = False
                    collect_frames = []
                    silence_count = 0

        if _gate is not None:
            print(f"[AEC]  {_gate.summary()}", flush=True)   # one bounded line per reply
        mon.stop_stream()
        mon.close()
    except Exception as e:
        print(f"[INT]  Monitor error: {e}", flush=True)


# ── PCM playback ──────────────────────────────────────────────────────────────

def play_pcm(pcm_bytes: bytes, pa, rate: int = 48000):
    """Play mono s16le PCM through the wm8960 headphone output (stereo-expanded)."""
    _stop_playback.clear()
    raw = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    samples = np.clip(raw * 1.0, -32768, 32767).astype(np.int16)
    stereo = np.column_stack([samples, samples]).flatten().tobytes()
    interrupted = threading.Event()
    pos = [0]

    def callback(in_data, frame_count, time_info, status):
        if interrupted.is_set() or _stop_playback.is_set() or button_pressed():
            interrupted.set()
            return (b"\x00" * frame_count * 4, pyaudio.paComplete)
        chunk = stereo[pos[0]:pos[0] + frame_count * 4]
        pos[0] += frame_count * 4
        if len(chunk) < frame_count * 4:
            return (chunk + b"\x00" * (frame_count * 4 - len(chunk)), pyaudio.paComplete)
        return (chunk, pyaudio.paContinue)

    _int_stop = threading.Event()
    _int_thread = threading.Thread(
        target=_playback_interrupt_listener,
        args=(pa, _int_stop, interrupted),
        daemon=True,
    )
    _int_thread.start()

    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate,
                     output=True, frames_per_buffer=512,
                     stream_callback=callback)
    stream.start_stream()
    while stream.is_active():
        time.sleep(0.02)
        if button_pressed() or _stop_playback.is_set():
            interrupted.set()
    stream.stop_stream()
    stream.close()

    _int_stop.set()
    _int_thread.join(timeout=1.0)

    was_interrupted = interrupted.is_set()
    if was_interrupted:
        print("[STOP] Playback interrupted", flush=True)
    _stop_playback.clear()
    return was_interrupted


_EMOTION_SPEAK_FRAMES = {
    'NEUTRAL':   [0, 5, 0, 5],
    'HAPPY':     [1, 5, 1, 5],
    'CURIOUS':   [2, 5, 2, 0],
    'ANGRY':     [3, 0, 3, 0],
    'SLEEPY':    [4, 0, 4, 0],
    'SURPRISED': [5, 0, 5, 0],
    'SAD':       [6, 0, 6, 0],
    'CONFUSED':  [7, 0, 7, 5],
    'AMUSED':    [2, 0, 2, 5],
}

# RD-044 lip-sync: mouth animation is driven by a (time_sec, sprite_idx) timeline
# fired off actual playback position. _MOUTH_REST is the closed/resting sprite the
# jaw returns to between words and during the pre-speech settle.
_MOUTH_REST      = 0      # NEUTRAL (matches viseme_map.MOUTH_CLOSED)
_MOUTH_LEAD_S    = 0.0    # extra visual lead beyond ALSA output latency (tunable)
_LEGACY_TICK_S   = 0.50   # fixed-timer fallback cadence when no word timing exists


def _legacy_timeline(duration_s: float, frames):
    """Build a fixed-0.5s cycling timeline (old blind behaviour) for one blob when
    word timestamps are unavailable (Piper fallback or a failed captioned call).
    Keeps the position-driven player on a single uniform code path."""
    tl = []
    t = 0.0
    i = 0
    while t < max(duration_s, 0.001):
        tl.append((round(t, 4), frames[i % len(frames)]))
        t += _LEGACY_TICK_S
        i += 1
    return tl


def play_pcm_speaking(pcm_bytes: bytes, pa, teensy, emotion: str = 'NEUTRAL',
                      restore_mouth_idx: int = 0, rate: int = 48000) -> bool:
    """play_pcm with emotion-driven mouth animation. Cycles per-emotion frames at 0.50 s/frame.
    Suspends Person Sensor eye tracking during playback to prevent jitter.
    Returns True if playback was interrupted mid-stream."""
    frames = _EMOTION_SPEAK_FRAMES.get(emotion.upper(), _EMOTION_SPEAK_FRAMES['NEUTRAL'])
    stop_evt = threading.Event()

    def _animate():
        i = 0
        while not stop_evt.wait(0.50):
            teensy.send_command(f"MOUTH:{frames[i % len(frames)]}")
            i += 1
        teensy.send_command(f"MOUTH:{restore_mouth_idx}")

    teensy.send_command("EYES:SPEAKING")
    time.sleep(0.35)
    t = threading.Thread(target=_animate, daemon=True)
    t.start()
    was_interrupted = play_pcm(pcm_bytes, pa, rate)
    stop_evt.set()
    t.join(timeout=1.0)
    teensy.send_command("EYES:SPEAKING:STOP")
    return was_interrupted


def play_pcm_stream(pcm_queue, pa, teensy, emotion: str = 'NEUTRAL',
                    restore_mouth_idx: int = 0, rate: int = 48000,
                    interrupted: threading.Event | None = None,
                    stats: dict | None = None) -> bool:
    """
    Gapless playback of a stream of PCM blobs pulled from pcm_queue (queue.Queue).

    Producer thread puts s16le mono PCM bytes on the queue as each sentence is
    synthesized; a None sentinel signals end-of-stream. Unlike play_pcm_speaking
    (one blob), this sets EYES:SPEAKING once, runs a single continuous mouth
    animation and a single interrupt listener (one bleed baseline) spanning the
    whole multi-sentence utterance, and plays blobs back-to-back so audio starts
    on the first sentence while later sentences are still being generated/synthesized.

    `interrupted` may be passed in by the producer so it can observe the player's
    interrupt state while it is still consuming the LLM stream / synthesizing.

    This function does NOT touch _stop_playback's set/clear lifecycle: the
    producer owns clearing it at turn start and turn end. (It used to clear on
    entry and exit, which raced the producer's per-sentence STOP check — the
    flag vanished before the producer, blocked in synthesize(), could see it.)

    Returns True if playback was interrupted mid-stream (stop phrase, loud stop,
    button, or _stop_playback set externally).

    If `stats` (a dict) is passed, P4 inter-sentence gap telemetry is written
    into it at drain: blobs_played, gap_count, gap_total_ms, gap_max_ms (D1).
    """
    frames = _EMOTION_SPEAK_FRAMES.get(emotion.upper(), _EMOTION_SPEAK_FRAMES['NEUTRAL'])
    if interrupted is None:
        interrupted = threading.Event()

    # S214 A8: reference tap for software echo cancellation. Created only when
    # BARGEIN_AEC_ENABLED (default OFF); the listener runs the raw pre-A8 path
    # whenever this is None. docs/S214_A8_design.md §2.
    _ref_tap = None
    try:
        from hardware.aec import aec_enabled, RefTap
        if aec_enabled(_BARGEIN_CONFIG_PATH):
            _ref_tap = RefTap(rate_in=rate, rate_out=SAMPLE_RATE)
    except Exception as _e:
        print(f"[AEC]  tap init failed ({_e}) -- raw path", flush=True)
        _ref_tap = None

    # Single interrupt listener for the whole utterance (measures bleed baseline once)
    _int_stop = threading.Event()
    _int_thread = threading.Thread(
        target=_playback_interrupt_listener,
        args=(pa, _int_stop, interrupted, _ref_tap),
        daemon=True,
    )
    _int_thread.start()

    teensy.send_command("EYES:SPEAKING")
    teensy.send_command(f"MOUTH:{_MOUTH_REST}")   # rest closed during the settle
    time.sleep(0.35)

    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate,
                     output=True, frames_per_buffer=512)
    _SLICE = 512 * 4  # bytes per blocking-write slice (512 frames * 2ch * 2B)
    _BYTES_PER_SEC_MONO = rate * 2  # s16le mono blob -> seconds

    # ── RD-044 position-driven mouth scheduler ──────────────────────────────
    # Each queue item carries an optional (time_sec, sprite_idx) timeline built
    # from Kokoro's real per-word timestamps. Events fire when actual playback
    # position crosses their time, so the mouth tracks the audio instead of a
    # blind wall clock. `frames_written` leads what is HEARD by the ALSA output
    # latency, so we subtract it as the visual lead.
    try:
        _out_latency = float(stream.get_output_latency())
    except Exception:
        _out_latency = 0.0
    _lead = _out_latency + _MOUTH_LEAD_S
    frames_written = 0          # stereo frames handed to ALSA (== mono samples)
    schedule = []               # sorted [(abs_time_sec, idx)] across all blobs
    _sched = {"i": 0, "last": _MOUTH_REST}

    def _fire_due():
        heard_t = frames_written / rate - _lead
        i = _sched["i"]
        while i < len(schedule) and schedule[i][0] <= heard_t:
            idx = schedule[i][1]
            i += 1
            if idx != _sched["last"]:
                teensy.send_command(f"MOUTH:{idx}")
                _sched["last"] = idx
        _sched["i"] = i

    def _drain():
        while True:
            try:
                if pcm_queue.get_nowait() is None:
                    break
            except queue.Empty:
                break

    # ── P4 inter-sentence gap telemetry (S192f, audit AUD-8/D1) ──────────────
    # A gap is dead-air risk: after playback has started, if the producer hasn't
    # supplied the next blob yet, the blocking get() below waits -- and once
    # ALSA's ~output-latency buffer drains, that wait is audible silence between
    # sentences. We time each get() (cheap, off the write path) and count the
    # ones that block after the first blob. The first get() is the pre-playback
    # wait (TTFA), not a gap, so it's excluded. The end-of-stream sentinel isn't
    # a gap either. Bounded: three ints summarized once per utterance -- no
    # per-frame logging (respects feedback_no_unbounded_logging).
    _blobs_played = 0
    _gap_count = 0
    _gap_total_ms = 0.0
    _gap_max_ms = 0.0

    try:
        while True:
            _g0 = time.monotonic()
            item = pcm_queue.get()
            _gap_ms = (time.monotonic() - _g0) * 1000.0
            if item is None:
                break
            if _blobs_played > 0 and _gap_ms > 1.0:
                _gap_count += 1
                _gap_total_ms += _gap_ms
                if _gap_ms > _gap_max_ms:
                    _gap_max_ms = _gap_ms
            if interrupted.is_set() or _stop_playback.is_set() or button_pressed():
                interrupted.set()
                _drain()
                break
            # Item is (pcm_bytes, timeline) from the RD-044 producer; tolerate a
            # bare bytes blob too (legacy callers) so nothing breaks mid-migration.
            if isinstance(item, tuple):
                blob, timeline = item
            else:
                blob, timeline = item, None
            # Merge this blob's timeline into the absolute schedule at the audio
            # offset where the blob begins (monotonic -> schedule stays sorted).
            base = frames_written / rate
            if timeline is None:
                timeline = _legacy_timeline(len(blob) / _BYTES_PER_SEC_MONO, frames)
            for _t, _idx in timeline:
                schedule.append((base + _t, _idx))

            raw = np.frombuffer(blob, dtype=np.int16).astype(np.float32)
            samples = np.clip(raw, -32768, 32767).astype(np.int16)
            stereo = np.column_stack([samples, samples]).flatten().tobytes()
            pos = 0
            while pos < len(stereo):
                if interrupted.is_set() or _stop_playback.is_set() or button_pressed():
                    interrupted.set()
                    break
                _slice_bytes = stereo[pos:pos + _SLICE]
                stream.write(_slice_bytes)
                if _ref_tap is not None:
                    # S214 A8: reference push right after the ALSA hand-off -- the
                    # tap decimates to 16k and refreshes the heard-position anchor.
                    _ref_tap.push(samples[pos // 4: pos // 4 + len(_slice_bytes) // 4],
                                  _out_latency)
                pos += _SLICE
                frames_written += _SLICE // 4
                _fire_due()
            _blobs_played += 1
            if interrupted.is_set():
                _drain()
                break
    finally:
        # Flush ALSA's output buffer before stopping. In blocking mode
        # stream.write() returns once data is handed to ALSA, not when it has
        # actually been played out; stop_stream() then drops the last unplayed
        # period and clips the final syllable of the utterance. Writing a short
        # silence tail clocks the real samples through the buffer first. Skip
        # when interrupted -- there we WANT an immediate cut. (Inter-sentence
        # writes are contiguous, so only the final blob is at risk.)
        if not interrupted.is_set():
            _tail = b"\x00" * (rate * 4 // 5)  # ~200 ms stereo s16le silence
            try:
                stream.write(_tail)
                frames_written += len(_tail) // 4
                if _ref_tap is not None:
                    _ref_tap.push(np.zeros(len(_tail) // 4, dtype=np.int16), _out_latency)
            except Exception:
                pass
            _fire_due()  # let the final word's closure land as the tail clocks through
        stream.stop_stream()
        stream.close()
        # Smooth exit: close the jaw before restoring the resting/emotion sprite so
        # the rest->speaking->rest boundary never snaps from an open frame. On an
        # interrupt this still cleanly closes+restores (no stuck-open mouth).
        if _sched["last"] != _MOUTH_REST:
            teensy.send_command(f"MOUTH:{_MOUTH_REST}")
        teensy.send_command(f"MOUTH:{restore_mouth_idx}")
        teensy.send_command("EYES:SPEAKING:STOP")
        _int_stop.set()
        _int_thread.join(timeout=1.0)

    was_interrupted = interrupted.is_set()
    if was_interrupted:
        print("[STOP] Streaming playback interrupted", flush=True)

    # P4 gap summary: one bounded line per utterance + optional stats dict for
    # the bench row (S192f, D1). gap_total_ms is the total dead-air-risk time
    # spent waiting on the producer after playback had already begun.
    _gt = round(_gap_total_ms)
    _gm = round(_gap_max_ms)
    print(f"[BENCH] stage=gap_summary blobs_played={_blobs_played} "
          f"gap_count={_gap_count} gap_total_ms={_gt} gap_max_ms={_gm}", flush=True)
    if stats is not None:
        stats["blobs_played"] = _blobs_played
        stats["gap_count"] = _gap_count
        stats["gap_total_ms"] = _gt
        stats["gap_max_ms"] = _gm
    return was_interrupted


# ── Beeps ─────────────────────────────────────────────────────────────────────

def play_beep(pa):
    rate = 48000
    t = np.linspace(0, 0.2, int(rate * 0.2), False)
    tone = (np.sin(2 * np.pi * 880 * t) * 6000).astype(np.int16)
    stereo = np.column_stack([tone, tone]).flatten()
    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate, output=True)
    stream.write(stereo.tobytes())
    stream.stop_stream()
    stream.close()


def play_double_beep(pa):
    rate = 48000
    t = np.linspace(0, 0.12, int(rate * 0.12), False)
    tone = (np.sin(2 * np.pi * 660 * t) * 4000).astype(np.int16)
    gap = np.zeros(int(rate * 0.08), dtype=np.int16)
    sequence = np.concatenate([tone, gap, tone])
    stereo = np.column_stack([sequence, sequence]).flatten()
    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate, output=True)
    stream.write(stereo.tobytes())
    stream.stop_stream()
    stream.close()


def play_wol_beep(pa):
    # Ascending 2-tone: 660 Hz -> 880 Hz. Signals GandalfAI wake in progress.
    rate = 48000
    t1 = np.linspace(0, 0.15, int(rate * 0.15), False)
    t2 = np.linspace(0, 0.15, int(rate * 0.15), False)
    tone1 = (np.sin(2 * np.pi * 660 * t1) * 6000).astype(np.int16)
    gap   = np.zeros(int(rate * 0.06), dtype=np.int16)
    tone2 = (np.sin(2 * np.pi * 880 * t2) * 6000).astype(np.int16)
    sequence = np.concatenate([tone1, gap, tone2])
    stereo = np.column_stack([sequence, sequence]).flatten()
    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate, output=True)
    stream.write(stereo.tobytes())
    stream.stop_stream()
    stream.close()


def play_endpoint_cue(pa):
    """RD-047 T2 -- the endpoint-close cue.

    Fires the instant record_command() returns, BEFORE STT is submitted. The
    child's longest unlabeled silence is the endpoint wait plus the STT
    round-trip, and nothing currently marks its end; an unlabeled gap gets
    filled with a child's default reading of it ("she didn't hear me").

    Two soft descending blips with an exponential decay envelope (~130 ms, no
    click). Deliberately Pi-local synthesized audio: a signal ABOUT latency must
    never itself depend on the network, GandalfAI, or Kokoro.
    """
    rate = 48000

    def _blip(freq, dur, amp):
        t = np.linspace(0, dur, int(rate * dur), False)
        env = np.exp(-t / (dur * 0.35))          # fast decay -> "mm!", not a beep
        return (np.sin(2 * np.pi * freq * t) * env * amp).astype(np.int16)

    sequence = np.concatenate([_blip(780, 0.055, 3200), _blip(620, 0.070, 2800)])
    stereo = np.column_stack([sequence, sequence]).flatten()
    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate, output=True)
    stream.write(stereo.tobytes())
    stream.stop_stream()
    stream.close()


def play_your_turn_cue(pa) -> None:
    """S199 T4 -- the reciprocity ("your turn") cue.

    Rising two-blip mirror of play_endpoint_cue's falling pair: falling means
    "got it", rising means "your go". Same Pi-local synthesis rationale -- a
    turn-taking signal must never depend on the network, GandalfAI, or Kokoro.
    Contract: docs/S199_kids_tempo_contract.md rung 7."""
    rate = 48000

    def _blip(freq, dur, amp):
        t = np.linspace(0, dur, int(rate * dur), False)
        env = np.exp(-t / (dur * 0.35))
        return (np.sin(2 * np.pi * freq * t) * env * amp).astype(np.int16)

    sequence = np.concatenate([_blip(620, 0.055, 2800), _blip(820, 0.070, 3200)])
    stereo = np.column_stack([sequence, sequence]).flatten()
    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate, output=True)
    stream.write(stereo.tobytes())
    stream.stop_stream()
    stream.close()


# ── Record ────────────────────────────────────────────────────────────────────

def record_command(mic, ptt_mode: bool = False, kids_mode: bool = False):
    """
    Record from mic until adaptive silence close, no-speech exit, or max cap.
    kids_mode — when True uses KIDS_* thresholds from config.
    ptt_mode  — when True records until button released.
    Returns (raw_pcm_bytes, reason, secs): reason is one of
    'silence' | 'nospeech' | 'cap' | 'ptt'; secs is the true wall duration
    (chunk count based, channel-correct).

    S199 T6: the fixed RMS floors (SILENCE_RMS/KIDS_SILENCE_RMS) sat below
    real-room ambient, so the silence close NEVER fired -- every recording in
    the 72h journal ran to its cap. The floor is now relative to a measured
    ambient baseline (20th-percentile chunk RMS), the same noise-relative
    pattern the S195 barge-in uses (bleed_rms x 1.5). The trailing close is
    disarmed until speech onset so a thinking child is never clipped, and a
    no-speech exit recovers false wakes in ENDPOINT_NOSPEECH_SECS instead of
    the full cap. Timing contract: docs/S199_kids_tempo_contract.md.
    """
    frames = []
    # Re-read per call instead of using the module-level import binding. `from
    # core.config import X` copies the VALUE at import, so reload_overrides()
    # (UDP RELOAD_CONFIG, S192b) rebinds core.config's globals and this module
    # never sees it -- a WebUI change to the kids endpoint tunables silently did
    # nothing until a service restart. Same fix and same reason as
    # services/tts.py re-importing KOKORO_VOICE/KOKORO_SPEED per call. (RD-047)
    import core.config as _cfg
    rec_secs   = _cfg.KIDS_RECORD_SECONDS if kids_mode else _cfg.RECORD_SECONDS
    sil_secs   = _cfg.KIDS_SILENCE_SECS   if kids_mode else _cfg.SILENCE_SECS
    sil_floor  = _cfg.KIDS_SILENCE_RMS    if kids_mode else _cfg.SILENCE_RMS
    cps            = SAMPLE_RATE / CHUNK                    # chunks per second
    max_chunks     = int(cps * rec_secs)
    sil_limit      = max(1, int(cps * sil_secs))
    onset_need     = max(1, int(cps * _cfg.ENDPOINT_ONSET_MIN_MS / 1000.0))
    nospeech_limit = int(cps * _cfg.ENDPOINT_NOSPEECH_SECS)
    rms_hist = []
    speech_started = False
    onset_run = 0
    silence = 0
    _sil_peak = 0            # S201 (A5): peak trailing-silence counter, for ENDPOINT_DEBUG
    sil_rms = 0.0            # S201 (A5): last silence floor, for ENDPOINT_DEBUG (0 until onset)
    reason = "cap"
    n = 0
    for n in range(1, max_chunks + 1):
        f = mic.read(CHUNK, exception_on_overflow=False)
        frames.append(f)
        if ptt_mode:
            if not button_pressed():
                reason = "ptt"
                break
            continue
        rms = float(np.sqrt(np.mean(np.frombuffer(f, dtype=np.int16).astype(np.float32) ** 2)))
        # Ambient estimate: 20th-percentile of PRE-ONSET chunk RMS, FROZEN once
        # speech starts. Feeding speech chunks into the history dragged the
        # percentile up to speech level and the silence floor then ate the
        # speech -- a long-talking kid was clipped ~2s in (caught by fake-mic
        # simulation at the S199 deploy, before any kid hit it).
        if not speech_started:
            rms_hist.append(rms)
        baseline = float(np.percentile(rms_hist, 20)) if len(rms_hist) >= 3 else None
        if not speech_started:
            if baseline is not None and rms > max(sil_floor, baseline * _cfg.ENDPOINT_SPEECH_MULT):
                onset_run += 1
                if onset_run >= onset_need:
                    speech_started = True
            else:
                onset_run = 0
            if not speech_started and n >= nospeech_limit:
                reason = "nospeech"
                break
            continue
        sil_rms = max(sil_floor, (baseline or 0.0) * _cfg.ENDPOINT_SILENCE_MULT)
        # S201 (A5): leaky close -- a single above-floor chunk (intermittent room
        # noise: sibling/TV/dog/breath) no longer ZEROES the counter; it costs
        # ENDPOINT_SILENCE_DECAY quiet-chunks of progress. A mostly-quiet room now
        # endpoints instead of running to cap (measured 53% of 72h recordings hit cap).
        silence = silence + 1 if rms < sil_rms else max(0, silence - _cfg.ENDPOINT_SILENCE_DECAY)
        if silence > _sil_peak:
            _sil_peak = silence
        if silence >= sil_limit:
            reason = "silence"
            break
    if _cfg.ENDPOINT_DEBUG:
        # S201 (A5): bounded (one line per recording, NOT per-chunk -- RD-031) endpoint
        # decision trace, default OFF. Makes a future cap diagnosable: room tone above
        # sil_rms (mode 2, leaky can't help) vs intermittent spikes (mode 1, leaky fixes).
        print(f"[REC-DBG] reason={reason} secs={len(frames)*CHUNK/SAMPLE_RATE:.2f} "
              f"baseline={(baseline or 0):.0f} sil_rms={sil_rms:.0f} sil_peak={_sil_peak}/{sil_limit} "
              f"kids={int(kids_mode)} decay={_cfg.ENDPOINT_SILENCE_DECAY}", flush=True)
    return b"".join(frames), reason, len(frames) * CHUNK / SAMPLE_RATE
