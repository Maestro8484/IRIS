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

# S201 (A3): kids-register reciprocity invites. iris-kids often hands the turn
# back with an imperative ('!' not '?') that the adult predicate misses, so kids
# replies under-triggered the follow-up loop ([FLWP] 17x against 33 LLM turns in
# the 72h journal). CONTAINED match (not just endswith) because the invite is
# frequently mid-sentence ("Tell me what you had for lunch!"). Kids mode only.
KIDS_FOLLOWUP_CUES = (
    "your turn", "you try", "your go", "you go", "now you",
    "what do you think", "what about you", "how about you",
    "tell me", "show me", "let's", "you pick", "you choose",
    "can you", "do you", "did you", "have you", "what would you",
    "guess", "what else", "which one", "ready",
)


def implies_followup(reply: str, in_game: bool = False, kids: bool = False) -> bool:
    """True if `reply` invites a follow-up turn: ends in '?', ends in one of a
    small set of inviting phrases, (in a camera game only) contains a
    game-continuation cue even without a '?', or (S201 A3: in kids mode) contains
    a kids-register reciprocity invite even without a '?'."""
    r = reply.strip()
    if r.endswith('?'):
        return True
    rl = r.lower()
    if any(rl.endswith(p) or rl.endswith(p + '.') for p in
           ("want me to", "shall i", "would you like me to", "let me know if", "go ahead")):
        return True
    if in_game and any(c in rl for c in GAME_CONTINUE_CUES):
        return True
    if kids and any(c in rl for c in KIDS_FOLLOWUP_CUES):
        return True
    return False


# ── User-turn shape (S240 RD-065) ───────────────────────────────────────────
# implies_followup() asks whether HER reply invites another turn. This asks the
# other half: did the USER's turn invite a reply at all? S221's session machine
# re-opened the follow-up window after EVERY reply, so a turn she was merely
# told -- "remember that today we are getting ready for Matt's wedding" -- got
# the double-beep and the flashing cyan cue as though an answer were expected.
# The operator's word for that on 2026-07-25 was "pressured reciprocity": 33
# windows in one morning, several of them after statements. With this predicate
# the window opens when either side invites it, so she still holds the floor
# after asking something and still answers a question put to her, while a
# statement ends the exchange.

# Question openings, for the many transcripts Whisper returns without the '?'.
# "any" is deliberately absent: the operator's "Any future houses, Joe, have to
# face north-south." is a statement, and it is the exact turn that must not
# re-open the window.
_USER_QUESTION_STARTS = frozenset({
    "what", "whats", "what's", "who", "whom", "whose", "when", "where", "why",
    "how", "hows", "how's", "which", "can", "cant", "can't", "could", "will",
    "would", "shall", "should", "do", "dont", "don't", "does", "did", "is",
    "isnt", "isn't", "are", "arent", "aren't", "am", "was", "were", "has",
    "have", "may", "might", "must",
})

# Imperatives that ask her to PRODUCE something. These invite a reply and a
# window even though they are not questions -- "tell me a story" has to be
# followable by "keep going" without a wakeword (S217). Instruction verbs that
# warrant an acknowledgment rather than a dialogue ("remember ...") are
# deliberately absent; that omission is the operator's own worked example.
_USER_REQUEST_STARTS = (
    "tell me", "tell us", "show me", "show us", "explain", "describe",
    "give me", "give us", "read me", "read us", "sing", "play", "list",
    "help me", "help us", "teach me", "teach us", "look up",
    "keep going", "go on", "continue", "carry on", "say more", "what else",
    "more", "again",
)


def user_invites_followup(text: str) -> bool:
    """True if the USER's turn invites a reply, so holding the mic open after
    answering it is conversation rather than pressure.

    True for questions (a '?' anywhere, or a question-word opening) and for
    requests that ask her to produce something. False for statements,
    acknowledgments, and instructions she is simply given.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if "?" in t:
        return True
    t = t.strip(".!,;:").strip()
    if not t:
        return False
    if any(t == p or t.startswith(p + " ") for p in _USER_REQUEST_STARTS):
        return True
    return t.split(" ", 1)[0].strip("'\"") in _USER_QUESTION_STARTS


# ── Selftest ────────────────────────────────────────────────────────────────
# `python3 core/speech_gates.py --selftest` -- runnable on the Pi against the
# deployed bytes, the same guard core/prompt.py and core/recall.py carry. The
# user_invites_followup corpus is the VERBATIM [STT] transcript of the operator's
# 2026-07-25 ear-test, so the predicate is measured against the morning that
# produced the complaint rather than against invented examples.

_SELFTEST_INVITES = [
    # (transcript, expected, note)
    ("Can you help give us tips on how to make our house not so hot?", True,  "question"),
    ("What are you?",                                    True,  "question"),
    ("How do your eyes work?",                           True,  "question"),
    ("What's the plan, Phil?",                           True,  "question"),
    ("What did we talk about yesterday?",                True,  "question"),
    ("Tell me a story about a unicorn and a dog and...", True,  "request, S217 story"),
    ("Keep going.",                                      True,  "S217 resume"),
    ("May?  It's your fault.  What happened?  May?",     True,  "'?' anywhere"),
    # The turns that must NOT re-open the window -- the operator's complaint.
    ("Remember that today we are getting ready for Matt's wedding, "
     "and remember that to your memory.",                False, "instruction, not a dialogue"),
    ("We are getting ready for Matt's wedding.",         False, "statement"),
    ("Any future houses, Joe, have to face north-south.", False, "statement opening on 'any'"),
    ("You're just so mean.",                             False, "statement"),
    ("Yo, Iris is being mean.",                          False, "statement"),
    ("Harris, the list is too long for you to be on the help with.", False, "statement"),
    ("because I'm mentioning that I'm the favorite.",    False, "statement"),
    ("I can't touch your mouth.",                        False, "statement"),
    ("Take a break.",                                    False, "reflex BREAK, never an invite"),
    ("Sounds good. Go to take a nap. Bugger off.",       False, "dismissal"),
    ("Got it.",                                          False, "acknowledgment"),
    ("Okay.",                                            False, "acknowledgment"),
    ("Thank you.",                                       False, "acknowledgment"),
    ("Go.",                                              False, "acknowledgment"),
    ("ah",                                               False, "STT junk"),
    ("so",                                               False, "STT junk"),
    ("up.",                                              False, "STT junk"),
    ("You",                                              False, "STT junk"),
    ("",                                                 False, "empty"),
]

# Guards that the S240 change did not disturb the gates it sits beside.
_SELFTEST_PHRASE = [
    ("stop",        STOP_PHRASES,         True,  "exact"),
    ("stop it",     STOP_PHRASES,         True,  "phrase + space"),
    ("stopwatch",   STOP_PHRASES,         False, "S192a AUD-1 fused word"),
    ("thank you",   FOLLOWUP_DISMISSALS,  True,  "exact"),
    ("coolant",     FOLLOWUP_DISMISSALS,  False, "S192a AUD-1 fused word"),
    # Recorded, not asserted as a bug: phrase_matches IS a prefix-plus-space
    # match, so "cool as ice" matches the "cool" dismissal. The docstring above
    # names it as a case the matcher avoids, which is inaccurate. Pre-existing,
    # untouched by S240, filed rather than changed mid-task.
    ("cool as ice", FOLLOWUP_DISMISSALS,  True,  "prefix+space: actual behavior"),
]

_SELFTEST_IMPLIES = [
    ("Anything else on your mind before then?", False, False, True,  "her question"),
    ("The navy one should do nicely.",          False, False, False, "her statement"),
    ("Tell me what you had for lunch!",         False, True,  True,  "S201 A3 kids cue"),
    ("Nope! Try again!",                        True,  False, True,  "S168 game cue"),
]


def _selftest() -> int:
    passed = failed = 0
    for text, expected, note in _SELFTEST_INVITES:
        got = user_invites_followup(text)
        ok = got is expected
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        if not ok:
            print(f"FAIL user_invites_followup({text!r}) -> {got}, want {expected}  [{note}]")
    for norm, phrases, expected, note in _SELFTEST_PHRASE:
        got = phrase_matches(norm, phrases)
        ok = got is expected
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        if not ok:
            print(f"FAIL phrase_matches({norm!r}) -> {got}, want {expected}  [{note}]")
    for reply, in_game, kids, expected, note in _SELFTEST_IMPLIES:
        got = implies_followup(reply, in_game=in_game, kids=kids)
        ok = got is expected
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
        if not ok:
            print(f"FAIL implies_followup({reply!r}) -> {got}, want {expected}  [{note}]")
    total = passed + failed
    print(f"speech_gates selftest: {passed}/{total} PASS  FAIL:{failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python3 core/speech_gates.py --selftest")
