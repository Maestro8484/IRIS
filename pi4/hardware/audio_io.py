"""
hardware/audio_io.py - Audio input/output and volume control
wm8960 HAT, PyAudio, PCM playback with interrupt detection, record, volume.

Key design notes:
- _stop_playback is a module-level Event; import it to call .set() from outside.
- record_command() takes kids_mode as an explicit parameter (not a global).
- play_pcm_speaking() takes a TeensyBridge instance for mouth animation.
- _playback_interrupt_listener() uses adaptive baseline to ignore speaker bleed.
"""

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

# Stop phrases checked via lightweight STT during playback
STOP_PHRASES = {
    "stop", "cancel", "nevermind", "never mind", "quiet", "shut up",
    "be quiet", "stop talking", "that's enough", "enough", "hey jarvis",
    "jarvis stop", "ok stop", "please stop",
}

# Polite filler responses that end the follow-up loop without LLM processing
FOLLOWUP_DISMISSALS = {
    "thank you", "thanks", "thank you very much", "thanks very much",
    "thank you so much", "thanks so much",
    "ok", "okay", "ok thanks", "okay thanks", "ok thank you", "okay thank you",
    "great", "great thanks", "great thank you", "sounds great",
    "got it", "got it thanks", "got it thank you",
    "alright", "all right", "alright thanks", "sounds good", "perfect",
    "no", "no thanks", "no thank you", "nope", "that's all", "that is all",
    "that's it", "that is it", "i'm good", "im good", "i'm all good",
    "cool", "cool thanks", "awesome", "wonderful", "excellent",
}


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
    floor = 0 if allow_zero else VOL_MIN
    level = max(floor, min(VOL_MAX, level))
    subprocess.run(
        ["amixer", "-c", str(_find_wm8960_card()), "sset", VOL_CONTROL, str(level)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return level


def handle_volume_command(text: str) -> str | None:
    """Handle voice volume commands. Returns response string or None if not a volume command."""
    t = text.lower().strip().rstrip(".!?")
    pct_match = re.search(r'(\d+)\s*(?:percent|%)', t)
    if pct_match and 'volume' in t:
        target = max(VOL_MIN, min(VOL_MAX, int(int(pct_match.group(1)) / 100 * VOL_MAX)))
        set_volume(target)
        return f"Volume set to {int(target / VOL_MAX * 100)} percent."
    if any(p in t for p in ("all the way up", "max volume", "volume max",
                             "full volume", "maximum volume", "as loud")):
        set_volume(VOL_MAX); return "Volume set to maximum."
    if any(p in t for p in ("all the way down", "volume low", "minimum volume",
                             "volume minimum", "as quiet")):
        set_volume(VOL_MIN); return "Volume set to minimum."
    if any(p in t for p in ("volume up", "louder", "turn it up", "increase volume",
                             "turn up", "raise volume", "higher volume", "more volume")):
        return f"Volume increased to {int(set_volume(get_volume() + VOL_STEP) / VOL_MAX * 100)} percent."
    if any(p in t for p in ("volume down", "quieter", "turn it down", "decrease volume",
                             "lower volume", "turn down", "reduce volume",
                             "less volume", "softer", "too loud")):
        return f"Volume decreased to {int(set_volume(get_volume() - VOL_STEP) / VOL_MAX * 100)} percent."
    if any(p in t for p in ("what's the volume", "whats the volume", "current volume",
                             "volume level", "how loud", "what volume")):
        return f"Volume is at {int(get_volume() / VOL_MAX * 100)} percent."
    if 'volume' in set(t.split()) and len(t.split()) <= 6:
        return f"Volume is at {int(get_volume() / VOL_MAX * 100)} percent."
    return None


# ── Playback interrupt listener ───────────────────────────────────────────────

def _playback_interrupt_listener(pa_ref, stop_event, interrupted_event):
    """
    Background thread: opens a separate mic stream during playback.
    Triggers interrupted_event if voice matches a STOP_PHRASES phrase via STT.

    Phase 1: measures speaker-bleed baseline (0.5 s).
    Phase 2: detects voice at bleed × 1.5; collects utterance; verifies via
             Wyoming Whisper STT. Fires interrupt only on STOP_PHRASES match.
    """
    _BASELINE_CHUNKS = int(SAMPLE_RATE / CHUNK * 0.5)
    _DETECT_MULTIPLIER = 1.5
    _COLLECT_CHUNKS    = int(SAMPLE_RATE / CHUNK * 1.5)   # max utterance length
    _SILENCE_CHUNKS    = int(SAMPLE_RATE / CHUNK * 0.30)  # trailing silence ends utterance

    try:
        mon = pa_ref.open(rate=SAMPLE_RATE, channels=CHANNELS,
                          format=pyaudio.paInt16, input=True,
                          frames_per_buffer=CHUNK)

        # Phase 1: measure speaker-bleed baseline
        baseline_vals = []
        for _ in range(_BASELINE_CHUNKS):
            if stop_event.is_set():
                break
            try:
                data = mon.read(CHUNK, exception_on_overflow=False)
                rms = np.sqrt(np.mean(
                    np.frombuffer(data, dtype=np.int16).astype(np.float32) ** 2))
                baseline_vals.append(rms)
            except Exception:
                break

        if baseline_vals:
            bleed_rms = float(np.percentile(baseline_vals, 90))
            detect_threshold = max(float(INTERRUPT_RMS_THRESHOLD),
                                   bleed_rms * _DETECT_MULTIPLIER)
        else:
            bleed_rms = 0.0
            detect_threshold = float(INTERRUPT_RMS_THRESHOLD)
        print(f"[INT]  Bleed baseline RMS={bleed_rms:.0f}  detect_threshold={detect_threshold:.0f}",
              flush=True)

        # Adaptive loud-stop floor: tracks highest bleed RMS seen while IRIS is speaking so
        # the instant-interrupt threshold stays above speaker bleed even when the 0.5s baseline
        # window started during an inter-sentence gap (giving a misleadingly low bleed_rms).
        peak_bleed = bleed_rms

        # Phase 2: collect voice utterance, verify stop phrase via STT
        collect_frames = []
        collecting = False
        silence_count = 0
        stt_pending = threading.Event()

        def _verify_stt(frames):
            try:
                import services.stt as _stt
                transcript = _stt.transcribe(b"".join(frames)).lower().strip()
                print(f"[INT]  STT: '{transcript}'", flush=True)
                if any(p in transcript for p in STOP_PHRASES):
                    print("[INT]  Stop phrase matched — firing interrupt", flush=True)
                    interrupted_event.set()
                    _stop_playback.set()
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

            # Update peak bleed from IRIS's speaker voice while not collecting user speech.
            # Any RMS above detect_threshold while idle is IRIS's bleed, not the user.
            if not collecting and rms > detect_threshold:
                peak_bleed = max(peak_bleed, rms)

            # Effective loud-stop floor adapts above observed speaker bleed (1.5× peak).
            # Prevents false instant-interrupt when IRIS's own voice spikes past the fixed
            # LOUD_STOP_THRESHOLD (seen at VOL_MAX=126 where bleed peaks at ~26000-27000).
            effective_loud_stop = max(LOUD_STOP_THRESHOLD, peak_bleed * 1.5)
            if rms > effective_loud_stop:
                print(f"[INT]  Loud stop triggered (RMS={rms:.0f} > {effective_loud_stop:.0f}) — instant interrupt", flush=True)
                interrupted_event.set()
                _stop_playback.set()
                break
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
                    interrupted: threading.Event | None = None) -> bool:
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
    """
    frames = _EMOTION_SPEAK_FRAMES.get(emotion.upper(), _EMOTION_SPEAK_FRAMES['NEUTRAL'])
    if interrupted is None:
        interrupted = threading.Event()

    # Single interrupt listener for the whole utterance (measures bleed baseline once)
    _int_stop = threading.Event()
    _int_thread = threading.Thread(
        target=_playback_interrupt_listener,
        args=(pa, _int_stop, interrupted),
        daemon=True,
    )
    _int_thread.start()

    # Single continuous mouth animation for the whole utterance
    anim_stop = threading.Event()

    def _animate():
        i = 0
        while not anim_stop.wait(0.50):
            teensy.send_command(f"MOUTH:{frames[i % len(frames)]}")
            i += 1
        teensy.send_command(f"MOUTH:{restore_mouth_idx}")

    teensy.send_command("EYES:SPEAKING")
    time.sleep(0.35)
    anim_thread = threading.Thread(target=_animate, daemon=True)
    anim_thread.start()

    stream = pa.open(format=pyaudio.paInt16, channels=2, rate=rate,
                     output=True, frames_per_buffer=512)
    _SLICE = 512 * 4  # bytes per blocking-write slice (512 frames * 2ch * 2B)

    def _drain():
        while True:
            try:
                if pcm_queue.get_nowait() is None:
                    break
            except queue.Empty:
                break

    try:
        while True:
            blob = pcm_queue.get()
            if blob is None:
                break
            if interrupted.is_set() or _stop_playback.is_set() or button_pressed():
                interrupted.set()
                _drain()
                break
            raw = np.frombuffer(blob, dtype=np.int16).astype(np.float32)
            samples = np.clip(raw, -32768, 32767).astype(np.int16)
            stereo = np.column_stack([samples, samples]).flatten().tobytes()
            pos = 0
            while pos < len(stereo):
                if interrupted.is_set() or _stop_playback.is_set() or button_pressed():
                    interrupted.set()
                    break
                stream.write(stereo[pos:pos + _SLICE])
                pos += _SLICE
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
            except Exception:
                pass
        stream.stop_stream()
        stream.close()
        anim_stop.set()
        anim_thread.join(timeout=1.0)
        teensy.send_command("EYES:SPEAKING:STOP")
        _int_stop.set()
        _int_thread.join(timeout=1.0)

    was_interrupted = interrupted.is_set()
    if was_interrupted:
        print("[STOP] Streaming playback interrupted", flush=True)
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


# ── Record ────────────────────────────────────────────────────────────────────

def record_command(mic, ptt_mode: bool = False, kids_mode: bool = False) -> bytes:
    """
    Record from mic until silence or max duration.
    kids_mode — when True uses KIDS_* thresholds from config.
    ptt_mode  — when True records until button released.
    Returns raw PCM bytes.
    """
    frames = []
    silence = 0
    rec_secs   = KIDS_RECORD_SECONDS if kids_mode else RECORD_SECONDS
    sil_secs   = KIDS_SILENCE_SECS   if kids_mode else SILENCE_SECS
    sil_rms    = KIDS_SILENCE_RMS    if kids_mode else SILENCE_RMS
    max_chunks = int(SAMPLE_RATE / CHUNK * rec_secs)
    sil_limit  = int(SAMPLE_RATE / CHUNK * sil_secs)
    for _ in range(max_chunks):
        f = mic.read(CHUNK, exception_on_overflow=False)
        frames.append(f)
        if ptt_mode:
            if not button_pressed():
                break
        else:
            rms = np.sqrt(np.mean(np.frombuffer(f, dtype=np.int16).astype(np.float32) ** 2))
            silence = silence + 1 if rms < sil_rms else 0
            if silence >= sil_limit:
                break
    return b"".join(frames)
