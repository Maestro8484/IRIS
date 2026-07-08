"""
core/speech_gates.py - Pure predicates for STOP/dismissal/hallucination gating
and follow-up-loop continuation, extracted from assistant.py + hardware/audio_io.py
(S192 AUD-7 test-suite session).

Behavior-identical extraction: no logic changed, only relocated so it can be
imported (and unit-tested on SuperMaster/Windows) without pulling in pyaudio
or RPi.GPIO. assistant.py's main loop and hardware/audio_io.py's playback
interrupt listener both import STOP_PHRASES / FOLLOWUP_DISMISSALS from here
now instead of defining them locally, so there is exactly one copy.

phrase_matches() is the word-boundary matcher used by the main-loop STOP gate
and (since the S192a AUD-1 fix) the follow-up-loop STOP/dismissal gates:
exact match or phrase-followed-by-space, so "stop" matches "stop" and
"stop it" but not "stopwatch". mirrors core/intent_router.py's _starts_phrase.

is_whisper_hallucination() is the union of the main-gate and follow-up-gate
hallucination filters (S191 audit AUD-1: these had drifted apart into two
separately-maintained inline tuples; now unified as one shared set/tuple pair
so both call sites filter identical junk).
"""

from __future__ import annotations

# ── STOP / dismissal phrase sets ────────────────────────────────────────────
# Checked via lightweight STT during playback (hardware/audio_io.py interrupt
# listener) and against the main-loop / follow-up-loop transcript (assistant.py).
STOP_PHRASES = {
    "stop", "cancel", "nevermind", "never mind", "quiet", "shut up",
    "be quiet", "stop talking", "that's enough", "enough", "hey jarvis",
    "jarvis stop", "ok stop", "please stop",
}

# Polite filler responses that end the follow-up loop without LLM processing.
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


def phrase_matches(norm: str, phrases) -> bool:
    """Word-boundary phrase match: exact match or phrase followed by a space.

    Avoids false matches on fused words like "stopwatch", "cool as ice"
    (S192a AUD-1: the follow-up loop used to do a bare startswith() here,
    which let "stopwatch" falsely end the conversation). `norm` should
    already be lowercased/stripped of trailing punctuation, matching how
    assistant.py builds `_text_norm`.
    """
    return any(norm == p or norm.startswith(p + " ") for p in phrases)


# ── Whisper hallucination gate ──────────────────────────────────────────────
# Short phrases Whisper hallucinates on silence/near-silence, and URL/spam
# patterns it hallucinates on noise. Union of the former main-gate and
# follow-up-gate lists (S191 audit AUD-1: they had drifted apart into two
# separately-maintained copies); both gates now share this one definition.
WHISPER_HALLUCINATIONS = {
    "thank you", "thanks", "thank you very much", "thanks for watching",
    "you", "the", "bye", "bye bye", "goodbye", "see you next time",
    "please subscribe", ".", "", " ",
}

WHISPER_HALLUCINATION_PATTERNS = (
    "for more information", "www.", ".gov", ".com", ".org",
    "subscribe", "don't forget",
)


def is_whisper_hallucination(norm: str) -> bool:
    """True if `norm` (lowercased/stripped transcript) matches a known Whisper
    hallucination -- either an exact short-phrase match or a substring/prefix
    pattern match. Mirrors the main-loop gate's exact `or` logic (startswith
    OR contains for the pattern tuple); the follow-up loop's two-step version
    (exact-set check, then separate pattern-contains check) is behaviorally
    equivalent to this single call, just split across two `if`s at the call
    site for early-break reasons -- see assistant.py's two follow-up checks.
    """
    return norm in WHISPER_HALLUCINATIONS or \
        any(norm.startswith(p) or p in norm for p in WHISPER_HALLUCINATION_PATTERNS)


# ── Follow-up continuation ──────────────────────────────────────────────────
# Cues that keep a reciprocal camera-game loop alive even when the reply
# doesn't end in '?' (e.g. "Nope! Try again!", "You got it!").
GAME_CONTINUE_CUES = (
    "try again", "guess again", "another", "one more", "keep going",
    "so close", "close", "nope", "not quite", "you got it", "got it",
    "your turn", "go again", "ready", "what else",
)


def implies_followup(reply: str, in_game: bool = False) -> bool:
    """True if `reply` invites a follow-up turn: ends in '?', ends in one of a
    small set of inviting phrases, or (in a camera game only) contains a
    game-continuation cue even without a '?'."""
    r = reply.strip()
    if r.endswith('?'):
        return True
    rl = r.lower()
    if any(rl.endswith(p) or rl.endswith(p + '.') for p in
           ("want me to", "shall i", "would you like me to", "let me know if", "go ahead")):
        return True
    if in_game and any(c in rl for c in GAME_CONTINUE_CUES):
        return True
    return False
