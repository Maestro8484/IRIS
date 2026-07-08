"""
services/tts.py - Text-to-speech (Kokoro primary, Wyoming Piper fallback)
Returns raw s16le PCM bytes at 48000 Hz mono.

synthesize(text) → bytes   — public entry point, routes Kokoro → Piper on failure
spoken_numbers(text) → str — pre-processes numeric tokens for natural TTS
"""

import json
import re
import socket

import requests

from core.config import (
    F5_BASE_URL, F5_ENABLED, F5_TIMEOUT,
    KOKORO_BASE_URL, KOKORO_ENABLED,
    GANDALF, PIPER_PORT, PIPER_VOICE,
    SAMPLE_RATE, CHANNELS, TTS_MAX_CHARS,
)
# KOKORO_VOICE is NOT imported at module level (unlike the above) -- it must be
# re-imported per call (see _synthesize_kokoro/_synthesize_kokoro_captioned)
# so a live core.config.reload_overrides() (S192b AUD-5) actually takes effect;
# `from X import Y` at module scope freezes Y at first-import time forever.
from services.wyoming import wy_send, read_line
from core.viseme_map import build_mouth_timeline
from core.normalize_tts import normalize_for_tts


# F5-TTS (voice-clone DNA, primary) ------------------------------------------------

def _synthesize_f5(text: str, speed: float | None = None) -> bytes:
    """F5-TTS /v1/audio/speech on GandalfAI:8005 (voice-clone voice DNA).
    Same WAV-response contract as Kokoro. Returns s16le PCM at 48000 Hz."""
    import miniaudio
    url = f"{F5_BASE_URL}/v1/audio/speech"
    payload = {
        "model": "f5-tts",
        "input": text,
        "response_format": "wav",
        "speed": speed if speed is not None else 1.0,
    }
    resp = requests.post(url, json=payload, timeout=F5_TIMEOUT)
    resp.raise_for_status()
    wav_bytes = resp.content
    if len(wav_bytes) < 44:
        raise RuntimeError(f"[F5] Response too short: {len(wav_bytes)} bytes")
    decoded = miniaudio.decode(
        wav_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=48000,
    )
    pcm = bytes(decoded.samples)
    print(f"[F5]   OK {len(wav_bytes)}b WAV -> {len(pcm)}b PCM ({decoded.duration:.1f}s)", flush=True)
    return pcm


# ── Kokoro TTS ────────────────────────────────────────────────────────────────

def _synthesize_kokoro(text: str, speed: float | None = None) -> bytes:
    """Kokoro-FastAPI /v1/audio/speech endpoint. Returns s16le PCM at 48000 Hz."""
    import miniaudio
    from core.config import KOKORO_SPEED, KOKORO_VOICE
    url = f"{KOKORO_BASE_URL}/v1/audio/speech"
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": KOKORO_VOICE,
        "response_format": "wav",
        "speed": speed if speed is not None else KOKORO_SPEED,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    wav_bytes = resp.content
    if len(wav_bytes) < 44:
        raise RuntimeError(f"[KOK] Response too short: {len(wav_bytes)} bytes")
    decoded = miniaudio.decode(
        wav_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=48000,
    )
    pcm = bytes(decoded.samples)
    print(f"[KOK]  OK {len(wav_bytes)}b WAV -> {len(pcm)}b PCM ({decoded.duration:.1f}s)", flush=True)
    return pcm


def _synthesize_kokoro_captioned(text: str, speed: float | None = None):
    """Kokoro-FastAPI /dev/captioned_speech — returns (pcm, word_timestamps).

    RD-044 (mouth lip-sync). Same voice/speed contract as _synthesize_kokoro, but
    the response is JSON: base64 WAV in 'audio' plus a per-word 'timestamps' list
    of {'word','start_time','end_time'} (relative to this utterance's audio start,
    verified live S189). Phase 0 bench: no measurable TTFA cost vs /v1/audio/speech
    idle or under LLM load. Raises on any failure so the caller can fall back to
    the plain (untimed) path."""
    import base64
    import miniaudio
    from core.config import KOKORO_SPEED, KOKORO_VOICE
    url = f"{KOKORO_BASE_URL}/dev/captioned_speech"
    payload = {
        "model": "kokoro",
        "input": text,
        "voice": KOKORO_VOICE,
        "response_format": "wav",
        "speed": speed if speed is not None else KOKORO_SPEED,
        "stream": False,
        "return_timestamps": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    b64 = data.get("audio")
    words = data.get("timestamps") or []
    if not b64:
        raise RuntimeError("[KOKCAP] no audio field in response")
    wav_bytes = base64.b64decode(b64)
    if len(wav_bytes) < 44:
        raise RuntimeError(f"[KOKCAP] audio too short: {len(wav_bytes)} bytes")
    decoded = miniaudio.decode(
        wav_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=48000,
    )
    pcm = bytes(decoded.samples)
    print(f"[KOKCAP] OK {len(pcm)}b PCM ({decoded.duration:.1f}s) words={len(words)}", flush=True)
    return pcm, words


# ── Piper (Wyoming) ───────────────────────────────────────────────────────────

def _synthesize_piper(text: str) -> bytes:
    """Piper TTS fallback via Wyoming protocol on GandalfAI."""
    with socket.create_connection((GANDALF, PIPER_PORT), timeout=60) as s:
        wy_send(s, "synthesize", {"text": text, "voice": {"name": PIPER_VOICE}})
        s.settimeout(60)
        audio_chunks = []
        buf = b""
        while True:
            line, buf = read_line(s, buf)
            hdr = json.loads(line.decode())
            etype = hdr.get("type", "")
            dlen = hdr.get("data_length", 0)
            plen = hdr.get("payload_length", 0)
            while len(buf) < dlen + plen:
                chunk = s.recv(8192)
                if not chunk:
                    raise RuntimeError("piper: connection closed mid-stream")
                buf += chunk
            pcm = buf[dlen:dlen + plen]
            buf = buf[dlen + plen:]
            if etype == "audio-chunk" and pcm:
                audio_chunks.append(pcm)
            elif etype == "audio-stop":
                import miniaudio
                raw = b"".join(audio_chunks)
                return bytes(miniaudio.convert_frames(
                    miniaudio.SampleFormat.SIGNED16, 1, 22050, raw,
                    miniaudio.SampleFormat.SIGNED16, 1, 48000,
                ))
            elif etype == "error":
                raise RuntimeError(f"Piper error: {hdr}")


# ── Number normalisation ──────────────────────────────────────────────────────

def spoken_numbers(text: str) -> str:
    """Convert numeric tokens to spoken English before TTS (no inflect dependency)."""
    _ONES = [
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen",
    ]
    _TENS = ["", "", "twenty", "thirty", "forty", "fifty",
             "sixty", "seventy", "eighty", "ninety"]

    def _int_to_words(n: int) -> str:
        if n < 0:
            return "negative " + _int_to_words(-n)
        if n < 20:
            return _ONES[n]
        if n < 100:
            rest = (" " + _ONES[n % 10]) if n % 10 else ""
            return _TENS[n // 10] + rest
        if n < 1000:
            rest = (" " + _int_to_words(n % 100)) if n % 100 else ""
            return _ONES[n // 100] + " hundred" + rest
        if n < 1_000_000:
            thousands = n // 1000
            remainder = n % 1000
            rest = (" " + _int_to_words(remainder)) if remainder else ""
            return _int_to_words(thousands) + " thousand" + rest
        if n < 1_000_000_000:
            millions = n // 1_000_000
            remainder = n % 1_000_000
            rest = (" " + _int_to_words(remainder)) if remainder else ""
            return _int_to_words(millions) + " million" + rest
        return str(n)

    text = re.sub(r'(\d+)\s*[°º]?F\b',
                  lambda m: _int_to_words(int(m.group(1))) + " degrees", text)
    text = re.sub(r'(\d+)\s*mph\b',
                  lambda m: _int_to_words(int(m.group(1))) + " miles per hour",
                  text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+)\s*%',
                  lambda m: _int_to_words(int(m.group(1))) + " percent", text)
    text = re.sub(r'\b(\d+)\b',
                  lambda m: _int_to_words(int(m.group(1))),
                  text)
    return text


# ── TTS input truncation ─────────────────────────────────────────────────────

def _truncate_for_tts(text: str, max_chars: int = TTS_MAX_CHARS) -> str:
    """
    Cap TTS input at max_chars to bound Chatterbox generation time.
    Truncates at the last sentence boundary (. ? !) before max_chars.
    If no boundary found, returns text untruncated to avoid mid-word cut.
    Default is TTS_MAX_CHARS from config (overridable via iris_config.json).
    """
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    for punct in ('.', '?', '!'):
        idx = window.rfind(punct)
        if idx > max_chars // 2:
            truncated = text[:idx + 1].strip()
            print(f"[TTS]  Truncated {len(text)}→{len(truncated)} chars at sentence boundary", flush=True)
            return truncated
    hard_cut = text[:max_chars]
    last_space = hard_cut.rfind(' ')
    if last_space > max_chars // 2:
        hard_cut = hard_cut[:last_space]
    print(f"[TTS]  No sentence boundary -- hard cap at {len(hard_cut)} chars", flush=True)
    return hard_cut


# ── Public entry point ────────────────────────────────────────────────────────

def _clean_tts_text(text: str) -> str:
    """Strip markdown / speech markers / non-ASCII and truncate for TTS. Shared by
    synthesize() and synthesize_captioned() so word timestamps align to the exact
    text that is actually spoken."""
    text = normalize_for_tts(text)                           # S194 Rung4: bench-proven normalizer (was spoken_numbers)
    text = re.sub(r'\*+', '', text)                          # bold/italic asterisks
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)      # _italic_ and __bold__
    text = re.sub(r'#+\s*', '', text)                        # headers
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)     # markdown links
    text = re.sub(r'`[^`]*`', '', text)                      # inline code
    text = re.sub(r'\[chuckle\]|\[laugh\]|\[sigh\]|\[gasp\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'[^\x00-\x7F]+', ' ', text).strip()      # existing non-ASCII strip (keep)
    return _truncate_for_tts(text)


def synthesize(text: str, speed: float | None = None) -> bytes:
    """Kokoro first; Piper fallback. Returns s16le PCM at 48000 Hz.
    Optional speed overrides KOKORO_SPEED (used by quip cache for faster wakeword responses)."""
    text = _clean_tts_text(text)

    if F5_ENABLED:
        try:
            return _synthesize_f5(text, speed=speed)
        except Exception as e:
            print(f"[F5]   Failed: {e} -- falling back to Kokoro", flush=True)
    if KOKORO_ENABLED:
        try:
            return _synthesize_kokoro(text, speed=speed)
        except Exception as e:
            print(f"[KOK]  Failed: {e} -- falling back to Piper", flush=True)
    return _synthesize_piper(text)


def synthesize_captioned(text: str, speed: float | None = None):
    """RD-044 word-timed variant. Returns (pcm_bytes, mouth_timeline) where
    mouth_timeline is a list of (time_sec, sprite_idx) built from Kokoro's real
    per-word timestamps, or None if word timing is unavailable for this utterance
    (F5 active, Kokoro disabled, or the captioned call failed) — in which case the
    caller falls back to the legacy fixed-timer mouth animation. PCM is always
    returned so the turn never goes silent on a timing failure."""
    cleaned = _clean_tts_text(text)
    # Word timing only exists on the Kokoro captioned endpoint. F5/Piper have none.
    if not F5_ENABLED and KOKORO_ENABLED:
        try:
            pcm, words = _synthesize_kokoro_captioned(cleaned, speed=speed)
            timeline = build_mouth_timeline(words)
            return pcm, (timeline or None)
        except Exception as e:
            print(f"[KOKCAP] Failed: {e} -- falling back to untimed synth", flush=True)
    # No timing path available: plain PCM, player uses legacy animation.
    return synthesize(text, speed=speed), None
