#!/usr/bin/env python3
"""
tools/gen_error_voice.py - pre-synthesize IRIS's degraded-state error lines.

Run ON THE PI, from /home/pi:

    python3 tools/gen_error_voice.py

Reads the lines from core.error_voice.LINES, synthesizes each with the LIVE
Kokoro voice/speed (services.tts.synthesize), and writes one WAV per line into
/home/pi/clips/errvoice/ . Re-run whenever KOKORO_VOICE or KOKORO_SPEED changes
-- the baked WAVs otherwise keep the old voice.

WAV format matches synthesize()'s output: mono, 48000 Hz, s16le. aplay resolves
the rate from the header, so these coexist with the 24 kHz soundboard clips.

Fail-soft: a line whose synth fails is skipped (logged); the others still write.
Exit code 0 only if every line was written.
"""

import os
import sys
import wave

# Run-from-anywhere: make /home/pi importable so `core`/`services` resolve the
# same way the assistant (WorkingDirectory=/home/pi) sees them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.error_voice import LINES, ERRVOICE_SUBDIR   # noqa: E402
from services.tts import synthesize                    # noqa: E402

CLIPS_ROOT = "/home/pi/clips"
OUT_DIR = os.path.join(CLIPS_ROOT, ERRVOICE_SUBDIR)


def _write_wav(path: str, pcm: bytes):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)       # s16le
        w.setframerate(48000)
        w.writeframes(pcm)


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    ok = 0
    for key, (basename, text) in LINES.items():
        try:
            pcm = synthesize(text)
        except Exception as e:
            print(f"[GEN]  {key}: synth FAILED ({e})", flush=True)
            continue
        if not pcm:
            print(f"[GEN]  {key}: empty PCM -- skipped", flush=True)
            continue
        out = os.path.join(OUT_DIR, basename)
        _write_wav(out, pcm)
        dur = len(pcm) / 2 / 48000.0
        print(f"[GEN]  {key}: {basename} ({dur:.1f}s, {len(pcm)}B PCM)", flush=True)
        ok += 1
    print(f"[GEN]  done: {ok}/{len(LINES)} written to {OUT_DIR}", flush=True)
    return 0 if ok == len(LINES) else 1


if __name__ == "__main__":
    sys.exit(main())
