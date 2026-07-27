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


# RD-047: the same failures, told to a 6-year-old. Same keys, same fail-quiet
# contract, different register -- honest about being a machine, funny about its
# own glitches, never silence. A child reads an unexplained silence as being
# ignored, which is worse than a wrong answer.
#
# The plan supplied four lines; GANDALF_WAKE_FAIL is written here so kids mode
# never drops into the adult register mid-failure. Any key absent from this dict
# degrades gracefully to the adult LINES entry.
KIDS_LINES = {
    "GANDALF_WAKING":    ("kids_gandalf_waking.wav",
                          "My big brain sleeps in the other room. It's waking up now, give it a minute."),
    "GANDALF_WAKE_FAIL": ("kids_gandalf_wake_fail.wav",
                          "My big brain won't wake up. Even robots have grumpy mornings. Try me in a minute?"),
    "LLM_DOWN":          ("kids_llm_down.wav",
                          "My thinking computer isn't answering. Even robots have days like Rusty."),
    "TTS_FAIL":          ("kids_tts_fail.wav",
                          "Something in me fell over. Dad'll fix it. He always does. Eventually."),
    "STT_FAIL":          ("kids_stt_fail.wav",
                          "Oops, my ears glitched. Robots do that. Say it again?"),
}


def _entry(key: str, kids: bool = False):
    """Resolve a key to (basename, text), preferring the kids line when asked."""
    if kids:
        entry = KIDS_LINES.get(key)
        if entry:
            return entry
    return LINES.get(key)


def line_text(key: str, kids: bool = False):
    """Plain text for a key (used by the generator). None if unknown."""
    entry = _entry(key, kids)
    return entry[1] if entry else None


def speak_error(key: str, stop_event=None, kids: bool = False) -> bool:
    """Play the pre-synthesized error line for `key`.

    `kids` selects the KIDS_LINES register when a kid line exists for that key,
    then falls back to the adult LINES entry -- if the kids WAV is missing or its
    playback fails, the adult twin (which may well exist) is tried before giving
    up (F4 / MAD). Never-silent beats in-register.

    Returns True if a clip actually played, False if the line is unknown or every
    candidate WAV is missing/failed. NEVER raises -- a total miss simply falls back
    to the caller's existing LED-only path, so wiring this in can only add speech,
    never remove existing behavior.
    """
    # Ordered candidates: kids WAV first (if asked and defined), then the adult WAV
    # for the same key. De-duplicated so a key present in only one table isn't tried
    # twice.
    candidates = []
    if kids and key in KIDS_LINES:
        candidates.append(KIDS_LINES[key])
    if key in LINES and (not candidates or LINES[key][0] != candidates[0][0]):
        candidates.append(LINES[key])
    if not candidates:
        return False
    for _basename, _text in candidates:
        filename = os.path.join(ERRVOICE_SUBDIR, _basename)
        try:
            if play_clip(filename, stop_event=stop_event):
                print(f"[ERRVOICE] spoke {key} ({_basename})", flush=True)
                return True
            print(f"[ERRVOICE] {key} ({_basename}) did not play (ALSA) -- trying next", flush=True)
        except Exception as e:
            print(f"[ERRVOICE] {key} ({_basename}) unavailable ({e}) -- trying next", flush=True)
    print(f"[ERRVOICE] {key} -- no candidate clip played, staying silent", flush=True)
    return False
