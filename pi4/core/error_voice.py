"""
core/error_voice.py - IRIS's spoken degraded-state lines (S194 Rung 6).

The "never silent" layer. When a conversational turn hits a dead end -- brain
asleep, LLM down, TTS dead, ears broken -- IRIS speaks a short in-character line
instead of just flashing the error LED and going quiet (the S194 Rung 1 audit's
F1/F2/F3 mute-robot points).

The lines are pre-synthesized to WAVs on the Pi (via tools/gen_error_voice.py,
Kokoro in the live voice/speed) and played through core.clip_player.play_clip --
ALSA-local, needing no live TTS and no GandalfAI. That is the whole point: the
failure being announced is usually the synthesis path or the brain itself, so
the announcement cannot depend on either. A Kokoro line cannot announce Kokoro's
own death; a baked WAV can.

Lines are data (LINES). Regenerate the WAVs whenever KOKORO_VOICE or KOKORO_SPEED
changes:

    python3 tools/gen_error_voice.py        # run on the Pi

Loader is FAIL-QUIET: a missing/corrupt WAV or any playback error degrades
silently to the old LED-only behavior. speak_error() NEVER raises.
"""

import os

from core.clip_player import play_clip

# Sub-directory under core.clip_player's clips root (/home/pi/clips).
ERRVOICE_SUBDIR = "errvoice"

# key -> (wav basename, spoken line). Lines: dry, British, hers -- IRIS keeps her
# composure even when a subsystem is down. Kept short so a fail state is a quick
# honest word, not a monologue.
LINES = {
    "GANDALF_WAKING":    ("gandalf_waking.wav",
                          "Hold on -- my brain's still booting up. Give me a tick."),
    "GANDALF_WAKE_FAIL": ("gandalf_wake_fail.wav",
                          "My brain's refusing to wake up just now. Give it a minute, then try me again."),
    "LLM_DOWN":          ("llm_down.wav",
                          "My thinking's gone offline for a moment. Try me again shortly."),
    "TTS_FAIL":          ("tts_fail.wav",
                          "I've got the words but no voice just now -- my mouth's misbehaving."),
    "STT_FAIL":          ("stt_fail.wav",
                          "I didn't quite catch that -- my ears aren't working properly right now."),
}


def line_text(key: str):
    """Plain text for a key (used by the generator). None if unknown."""
    entry = LINES.get(key)
    return entry[1] if entry else None


def speak_error(key: str, stop_event=None) -> bool:
    """Play the pre-synthesized error line for `key`.

    Returns True if a clip actually played, False if the line is unknown or the
    WAV is missing/failed. NEVER raises -- a missing WAV simply falls back to the
    caller's existing LED-only path, so wiring this in can only add speech, never
    remove existing behavior.
    """
    entry = LINES.get(key)
    if not entry:
        return False
    filename = os.path.join(ERRVOICE_SUBDIR, entry[0])
    try:
        played = play_clip(filename, stop_event=stop_event)
        if played:
            print(f"[ERRVOICE] spoke {key}", flush=True)
            return True
        # WAV present but ALSA failed on both devices -- report False so the caller
        # keeps its LED-only fallback (audio is dead anyway in this state).
        print(f"[ERRVOICE] {key} did not play (ALSA) -- staying silent", flush=True)
        return False
    except Exception as e:
        print(f"[ERRVOICE] {key} unavailable ({e}) -- staying silent", flush=True)
        return False
