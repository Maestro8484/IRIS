"""
hardware/aec.py - S214 A8: software acoustic echo cancellation for barge-in.

Cancels IRIS's own speaker output (the played reference PCM) out of the barge-in
listener's mic signal so a conversational-volume human voice becomes separable
from her speaker bleed. Design (read it before editing): docs/S214_A8_design.md.
Evidence that made it necessary: docs/S201_live_pipeline_findings.md (A8) +
docs/S214_A8_measurements.md (raised voice still below the bleed gate).

Three pieces:
- EchoCanceller: thin ctypes binding to the SYSTEM libspeexdsp.so.1 (already
  installed on the Pi; zero new packages). Speex MDF canceller, frame 256,
  tail 5120 (320 ms) at 16 kHz mono.
- RefTap: writer-side reference tap. play_pcm_stream pushes each mono 48 kHz
  slice right after handing it to ALSA; the tap low-passes (31-tap FIR, numpy)
  and decimates /3 into a 16 kHz ring, and publishes an atomic anchor
  (monotonic_time, samples_pushed_16k, alsa_output_latency_s) - the RD-044
  heard-position clock, reused.
- AecGate: listener-side pairing + confidence. Computes the mic-chunk <-> ref
  position offset ONCE from the anchor (mic and speaker share the wm8960 clock,
  so the offset is constant - no drift tracking), re-anchors if the pairing
  slips (silent mic overflow drops), and runs the ERLE watchdog that gates the
  presence tier / clean Vosk feed. Low confidence ALWAYS fails OPEN to the raw
  (pre-A8) listener behavior, never closed.

Everything here is inert unless BARGEIN_AEC_ENABLED (default False).
"""

import ctypes
import json
import time
from collections import deque

import numpy as np

_SPEEX_LIB = "libspeexdsp.so.1"
_SPEEX_ECHO_SET_SAMPLING_RATE = 24

AEC_FRAME = 256          # samples per speex frame (16 ms @ 16 kHz, power of two)
AEC_TAIL = 5120          # filter tail in samples (320 ms) - absorbs the alignment error budget
_LEAD = 480              # deliberate ref lead (30 ms): estimate error must never make ref lag its echo
_REANCHOR_SAMPLES = 2048 # pairing drift beyond this (128 ms) -> re-anchor + count it
_RING_SECONDS = 8
_ERLE_WINDOW_CHUNKS = 47 # ~3 s of 64 ms chunks
_ERLE_MIN_DB = 3.0       # below this after grace -> unconverged -> raw-path behavior
_ERLE_GRACE_S = 3.0
_ERLE_MIN_SAMPLES = 8    # audible chunks needed before ERLE means anything
_DIVERGE_MULT = 1.5      # clean persistently louder than raw x this -> filter diverged
_DIVERGE_CHUNKS = 8


def load_aec_config(config_path):
    """AEC keys read live from iris_config.json with core.config code defaults as
    fallback (same pattern as audio_io._load_bargein_config). Returns a dict."""
    import core.config as _cfg
    try:
        with open(config_path) as f:
            ov = json.load(f)
    except Exception:
        ov = {}

    def _get(key, default):
        v = ov.get(key, getattr(_cfg, key, default))
        return v

    return {
        "enabled":        bool(_get("BARGEIN_AEC_ENABLED", False)),
        "detect_mult":    float(_get("AEC_DETECT_MULT", 1.5)),
        "min_floor":      float(_get("AEC_MIN_FLOOR", 1500)),
        "presence":       bool(_get("BARGEIN_PRESENCE_ENABLED", True)),
        "presence_mult":  float(_get("BARGEIN_PRESENCE_MULT", 2.0)),
        "presence_ms":    float(_get("BARGEIN_PRESENCE_MS", 700)),
        "presence_kids":  bool(_get("BARGEIN_PRESENCE_KIDS", False)),
        "debug":          bool(_get("AEC_DEBUG", False)),
    }


def aec_enabled(config_path):
    """Cheap master-flag check for the writer-side tap decision."""
    try:
        return load_aec_config(config_path)["enabled"]
    except Exception:
        return False


def _load_speex():
    lib = ctypes.CDLL(_SPEEX_LIB)
    lib.speex_echo_state_init.restype = ctypes.c_void_p
    lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.speex_echo_cancellation.restype = None
    lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p]
    lib.speex_echo_ctl.restype = ctypes.c_int
    lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lib.speex_echo_state_destroy.restype = None
    lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
    return lib


class EchoCanceller:
    """Speex MDF echo canceller over the system libspeexdsp. int16 mono in/out."""

    def __init__(self, rate=16000, frame=AEC_FRAME, tail=AEC_TAIL):
        self._lib = _load_speex()
        self.frame = frame
        self._st = self._lib.speex_echo_state_init(frame, tail)
        if not self._st:
            raise RuntimeError("speex_echo_state_init failed")
        sr = ctypes.c_int(int(rate))
        self._lib.speex_echo_ctl(self._st, _SPEEX_ECHO_SET_SAMPLING_RATE, ctypes.byref(sr))

    def process(self, mic, ref):
        """Cancel ref (echo reference) out of mic. Both int16 arrays, equal length,
        a multiple of self.frame (the 1024-sample chunk = 4 frames)."""
        n = len(mic)
        out = np.empty(n, dtype=np.int16)
        f = self.frame
        for i in range(0, n - f + 1, f):
            m = np.ascontiguousarray(mic[i:i + f])
            r = np.ascontiguousarray(ref[i:i + f])
            o = np.empty(f, dtype=np.int16)
            self._lib.speex_echo_cancellation(
                self._st, m.ctypes.data, r.ctypes.data, o.ctypes.data)
            out[i:i + f] = o
        rem = n % f
        if rem:
            out[n - rem:] = mic[n - rem:]   # never happens with 1024/256; safe passthrough
        return out

    def close(self):
        if self._st:
            self._lib.speex_echo_state_destroy(self._st)
            self._st = None


class RefTap:
    """Writer-side reference tap: mono 48 kHz int16 slices in (as handed to ALSA),
    low-passed /3-decimated 16 kHz ring out, plus the atomic heard-position anchor.
    Single producer (playback thread), single consumer (listener thread)."""

    def __init__(self, rate_in=48000, rate_out=16000):
        if rate_in % rate_out:
            raise ValueError(f"non-integer decimation {rate_in}/{rate_out}")
        self._dec = rate_in // rate_out
        self.rate_out = rate_out
        # 31-tap windowed-sinc LPF, cutoff 0.45*rate_out (anti-alias before /3).
        fc = 0.45 * rate_out / rate_in     # cycles/sample at the input rate
        n = np.arange(31)
        h = 2 * fc * np.sinc(2 * fc * (n - 15)) * np.hamming(31)
        self._fir = (h / h.sum()).astype(np.float32)
        self._carry = np.zeros(30, dtype=np.float32)   # FIR state: last taps-1 inputs
        self._nin = 0                                  # total input samples consumed
        self._ring = np.zeros(rate_out * _RING_SECONDS, dtype=np.int16)
        self._wpos = 0                                 # absolute 16k samples pushed
        self.anchor = None                             # (monotonic, wpos_16k, out_latency_s)

    def push(self, mono48, out_latency_s):
        """Called by the playback thread right after stream.write() of this slice."""
        if len(mono48) == 0:
            return
        x = np.concatenate([self._carry, np.asarray(mono48, dtype=np.float32)])
        self._carry = x[-30:].copy()
        y = np.convolve(x, self._fir, mode="valid")    # len == len(mono48)
        start = (-self._nin) % self._dec               # keep global /3 phase across slices
        self._nin += len(mono48)
        d = y[start::self._dec]
        if len(d) == 0:
            self.anchor = (time.monotonic(), self._wpos, float(out_latency_s))
            return
        d16 = np.clip(d, -32768, 32767).astype(np.int16)
        rl = len(self._ring)
        w = self._wpos % rl
        k = min(len(d16), rl - w)
        self._ring[w:w + k] = d16[:k]
        if k < len(d16):
            self._ring[:len(d16) - k] = d16[k:]
        self._wpos += len(d16)
        self.anchor = (time.monotonic(), self._wpos, float(out_latency_s))

    def read(self, start_pos, n):
        """n samples at absolute 16k position start_pos; zeros outside the written
        (or still-retained) range. Tolerates the rare writer-overwrite race by
        clamping to the retained window - fail-open by construction."""
        out = np.zeros(n, dtype=np.int16)
        wpos = self._wpos
        rl = len(self._ring)
        lo = max(start_pos, wpos - rl, 0)
        hi = min(start_pos + n, wpos)
        if hi > lo:
            idx = np.arange(lo, hi) % rl
            out[lo - start_pos:hi - start_pos] = self._ring[idx]
        return out


class AecGate:
    """Listener-side pairing + confidence state for one playback/reply."""

    def __init__(self, cfg, kids, rate=16000, chunk=1024):
        self.cfg = cfg
        self.kids = kids
        self.rate = rate
        self.chunk_s = chunk / rate
        self.rpos = None            # next absolute 16k ref position to pair
        self.t0 = time.monotonic()
        self.reanchors = 0
        self.frames = 0
        self.unconverged_chunks = 0
        self.presence_fires = 0
        self.dead = False           # diverged/errored -> raw path for the reply
        self._win = deque(maxlen=_ERLE_WINDOW_CHUNKS)   # (raw_rms, clean_rms) on audible-raw chunks
        self._diverge_run = 0
        self._presence_win = deque(maxlen=64)   # per-chunk above-presence-floor bools
        self.clean_rms = 0.0
        self._erle_db = 0.0

    # ── pairing (the time-alignment core; see design doc §3) ──────────────────
    def fetch(self, tap, n, in_latency_s):
        """Aligned reference for the mic chunk just read (n mono samples). None =
        no reference available yet (raw path this chunk)."""
        a = tap.anchor
        if a is None:
            return None
        t_a, pushed, out_lat = a
        now = time.monotonic()
        # Ref index being HEARD right now (RD-044 clock, extrapolated to now),
        # minus the input path latency and the chunk span, plus the safety lead.
        expected = int(pushed - out_lat * self.rate + (now - t_a) * self.rate
                       - in_latency_s * self.rate - n + _LEAD)
        if self.rpos is None:
            self.rpos = expected
        elif abs(self.rpos - expected) > _REANCHOR_SAMPLES:
            print(f"[AEC]  re-anchor (pairing drift {self.rpos - expected:+d} samples"
                  f" -- mic overflow suspected)", flush=True)
            self.rpos = expected
            self.reanchors += 1
        ref = tap.read(self.rpos, n)
        self.rpos += n
        return ref

    # ── confidence / watchdog ─────────────────────────────────────────────────
    def update(self, raw_rms, clean_rms, audible_floor):
        self.frames += 1
        self.clean_rms = clean_rms
        if raw_rms > audible_floor:
            self._win.append((raw_rms, clean_rms))
            if clean_rms > raw_rms * _DIVERGE_MULT:
                self._diverge_run += 1
                if self._diverge_run >= _DIVERGE_CHUNKS:
                    self.dead = True
                    print("[AEC]  DIVERGED (clean persistently louder than raw)"
                          " -- raw path for the rest of this reply", flush=True)
            else:
                self._diverge_run = 0
        if self._win:
            raws = np.array([w[0] for w in self._win], dtype=np.float64)
            cleans = np.maximum([w[1] for w in self._win], 1.0)
            self._erle_db = float(np.mean(20.0 * np.log10(raws / cleans)))
        if not self.converged and (time.monotonic() - self.t0) > _ERLE_GRACE_S:
            self.unconverged_chunks += 1
        if self.cfg["debug"] and self.frames % 15 == 0:
            print(f"[AEC-DBG] raw={raw_rms:.0f} clean={clean_rms:.0f} "
                  f"erle={self._erle_db:.1f}dB floor={self.feed_floor:.0f} "
                  f"conv={int(self.converged)}", flush=True)

    @property
    def converged(self):
        if self.dead:
            return False
        if (time.monotonic() - self.t0) < _ERLE_GRACE_S:
            return False
        return len(self._win) >= _ERLE_MIN_SAMPLES and self._erle_db >= _ERLE_MIN_DB

    @property
    def feed_floor(self):
        """Vosk feed gate on the clean signal: trailing-min residual x mult.
        The trailing min over audible-raw chunks IS the cancellation residual
        (near-end speech only pushes clean_rms up), so this tracks convergence
        without a fixed delay."""
        if self._win:
            residual = float(min(w[1] for w in self._win))
        else:
            residual = 0.0
        return max(self.cfg["min_floor"], residual * self.cfg["detect_mult"])

    def presence_step(self, clean_rms):
        """True when sustained near-end speech should fire the presence STOP.
        Windowed duty-cycle test: >=60% of the trailing presence_ms window above
        the presence floor. A hard consecutive-chunk requirement never fires on
        real speech (sim-caught S214: syllabic envelope dips every ~300 ms reset
        it), while a cough or clank fills only a few chunks of the window."""
        if not self.cfg["presence"] or not self.converged:
            self._presence_win.clear()
            return False
        if self.kids and not self.cfg["presence_kids"]:
            return False
        self._presence_win.append(clean_rms > self.feed_floor * self.cfg["presence_mult"])
        need = max(4, int(round(self.cfg["presence_ms"] / 1000.0 / self.chunk_s)))
        if len(self._presence_win) >= need:
            recent = list(self._presence_win)[-need:]
            if sum(recent) >= 0.6 * need:
                self._presence_win.clear()
                self.presence_fires += 1
                return True
        return False

    def summary(self):
        return (f"frames={self.frames} erle_avg={self._erle_db:.1f}dB "
                f"reanchors={self.reanchors} unconverged_chunks={self.unconverged_chunks} "
                f"presence_fires={self.presence_fires} dead={int(self.dead)} "
                f"tail_ms={AEC_TAIL * 1000 // 16000}")
