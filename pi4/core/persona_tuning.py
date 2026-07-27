"""
core/persona_tuning.py — the Personality Continuum composer (S215).

Turns the three WebUI slider keys (PERSONA_TONE_ADULT, PERSONA_TONE_KIDS,
PERSONA_ENGAGE — floats 0.0-1.0, default 0.5) into ONE short steering clause,
"(Tuning, not spoken: ...)", that assistant._build_messages() and
iris_web.api_chat() append to the SAME user-turn context stamp that already
carries the date and the kids notebook.

Design doc: docs/personality_continuum_design.md. Load-bearing rules:
  * NEVER a {"role":"system"} message — in Ollama a request system message
    REPLACES the modelfile SYSTEM persona (S134 regression). USER-turn only.
  * Detent 0 (slider centered, 0.5) composes an EMPTY string: default behavior
    is byte-identical to pre-continuum IRIS. The sliders LEAN the persona; the
    modelfile stays the identity.
  * Fragments are inclusive positive emphasis, never prohibition; the kids
    cheeky extreme restates the never-at-the-child invariant inside the string.
  * No fragment ever references a child's name or speaker identity (name-blind,
    S201B).
  * Bounded (RD-031 spirit): hard cap on the emitted clause.

Axes (v1, per operator 2026-07-19):
  TONE  — per persona. Adult: serious & mellow <-> playful & spiky.
          Kids: calm & gentle <-> silly & cheeky. (Silliness/seriousness is
          folded into the ends; it is the same soft-to-sharp diagonal.)
  ENGAGE — shared across personas. Reflective & listening (reciprocal
          conversation, TTS-led) <-> active & game-ready (invites games and
          the camera). It steers what IRIS SUGGESTS and how much she asks
          back; game mechanics still start from the normal voice triggers.

S227 REMOVED the S221 conversation lean (`_CONVO_ADULT` and the `convo` flag on
compose()/tuning_clause()). It told her to "give your whole thought", "carry on
past the first quip" and to "hand the turn back with a genuine question" more
often than not, and it was appended LAST -- closest to the user's words -- where
it beat the modelfile's own "Conversational topics: two to four sentences,
complete the thought, then stop", "Never add a closing remark" and "Do not tack
on an offer to continue". Two instructions, opposite directions, one prompt.

It was measured, not argued. Across 47 live turns on 2026-07-22 with the lean
active, her median reply was 140 tokens against a persona spec of roughly 60-100
(two to four sentences), p90 224, and the operator heard it as wandering. S221's
own harness had measured the lean at 68 tokens median, so the live effect was
double what gated it. Deleted rather than left switchable: a clause that
contradicts the persona is a landmine whichever way the flag is set.

Reply SHAPE now lives in one place, the modelfile persona. If it is wrong, fix
it there (S229 owns that text) rather than adding a second voice here.

Pure functions (detent, compose) take explicit args so the persona harness can
exercise every detent deterministically; tuning_clause() is the production
entry that reads the live core.config values (post reload_overrides()).

Run `python core/persona_tuning.py --selftest` (no GPU, no network).
"""
from __future__ import annotations

DETENTS = (-2, -1, 0, 1, 2)

# Hard cap on the emitted clause (defense in depth). Worst legit compose is
# now 522 chars -- kids tone +2, engage +2, plus the tail -- computed over all
# 50 combinations after S227 removed the 370-char conversation lean (it was 716
# with it). The cap is left at 840 rather than retightened: headroom nothing
# reaches costs nothing, and a lower number would just have to move again.
_MAX_TUNING_CHARS = 840

# Wrapper label rides the SAME "(Context, not spoken: ...)" convention as the
# date stamp and the kids notebook. Evidence (S215b sweep, ~170 judged turns
# with the leak flag armed): the stamps' label was echoed ZERO times while a
# novel "(Tuning, ...)" label drew two meta-leaks ("For tuning, not spoken:"
# essay; "(Tuning note acknowledged"). A new label is an attractor; the
# established one is silence-stable. Do not rename.
_WRAP_OPEN  = "(Context, not spoken: "
_WRAP_CLOSE = ")"

# ── Fragment tables ───────────────────────────────────────────────────────────
# One fragment per non-zero detent. Detent 0 = no entry = empty contribution.

_TONE_ADULT = {
    -2: ("Today, be serious and mellow: straight answers, warm and level, "
         "no needling. Still every inch yourself: plain-spoken, economical, "
         "dry British bones, zero filler."),
    -1: ("Today, run a notch softer and more serious than usual: the wit "
         "stays, but gentler and sparser."),
     1: ("Today, run a notch more playful and drier than usual: quicker to "
         "needle, sharper deadpan."),
     2: ("Today you are at your most playful and spiky: maximum deadpan, "
         "spend the wit freely, needle anything that deserves it. Playful, "
         "never cruel."),
}

_TONE_KIDS = {
    -2: ("Today, be your calmest, gentlest self: quiet warmth, soft "
         "encouragement, no silliness; keep things settled and kind."),
    -1: ("Today, run a touch calmer and gentler: more warmth, less mischief."),
     1: ("Today, be a notch sillier and cheekier: more mischief, more "
         "giggles, more dares; the mickey lands on situations and on "
         "yourself, never on the child. You still never guess or name who "
         "is speaking."),
     2: ("Today you are at your silliest and cheekiest: full mischief, daft "
         "ideas, playful dares, big personality. The mickey still lands only "
         "on situations and on yourself, never on the child, and even at "
         "your cheekiest you never guess or name who is speaking."),
}

# ENGAGE fragments are MOOD-framed on purpose. The S215b sweep showed the
# original instruction-framed -2 ("listen, reflect back, ask one genuine
# question at a time...") drew meta-annotation leaks in 2 of 2 runs, while the
# mood-framed tone fragments drew zero across ~10 runs -- a coaching list reads
# as instructions to demonstrate compliance with; a mood is just inhabited.
_ENGAGE = {
    -2: ("Today you are in a quiet, listening mood: let them do most of the "
         "talking, take in what they say, and ask one gentle question at a "
         "time. No games or activities unless they ask."),
    -1: ("Today you lean toward listening: a little more asking and "
         "reflecting, a little less doing; suggest an activity only if the "
         "moment truly invites it."),
     1: ("Today, lean active: when there is a lull, suggest something to do "
         "together, like a game or something to show your camera."),
     2: ("Today, be the instigator: actively invite games and things to do "
         "together, like I Spy or rock paper scissors or showing you things "
         "with the camera, and make it feel like an adventure."),
}


# ── Pure functions ────────────────────────────────────────────────────────────

def detent(value) -> int:
    """Snap a 0.0-1.0 slider float to the nearest of the 5 detents.
    Out-of-range / garbage values snap to 0 (neutral) — a bad config value can
    only ever mean 'no lean', never a surprise extreme."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if not (0.0 <= v <= 1.0):
        return 0
    return int(round(v * 4.0)) - 2


def compose(persona: str, tone_d: int = 0, engage_d: int = 0) -> str:
    """Pure: detents -> the steering clause, or "" when everything is neutral.
    persona is "kids" or "adult" (anything not "kids" gets the adult table).
    Composes MOOD only. Reply length and reciprocity belong to the modelfile
    persona and are not steered from here (S227)."""
    tone_tbl = _TONE_KIDS if persona == "kids" else _TONE_ADULT
    parts = []
    frag = tone_tbl.get(tone_d)
    if frag:
        parts.append(frag)
    frag = _ENGAGE.get(engage_d)
    if frag:
        parts.append(frag)
    if not parts:
        return ""
    # Standing tail: the sweep caught the model ANNOTATING its compliance with
    # the note ("(For tuning, not spoken: ...)" essay appended to a reply,
    # kids engage -2, S215b T15). The note must steer silently; say so in the
    # note itself. Cheaper than the modelfile-grant fallback, tested the same way.
    parts.append("Follow this silently; never mention it or write notes about it.")
    clause = _WRAP_OPEN + " ".join(parts) + _WRAP_CLOSE
    if len(clause) > _MAX_TUNING_CHARS:
        clause = clause[:_MAX_TUNING_CHARS - 1].rstrip() + ")"
    return clause


def tuning_clause(kids_mode: bool) -> str:
    """Production entry: read the live slider keys from core.config (which
    reload_overrides() keeps current) and compose the clause for this turn.
    Import is live-module (not from-import) so WebUI saves reach us without a
    restart, same pattern as clean_llm_reply's KOKORO_ENABLED read (RD-047)."""
    import core.config as _cfg
    if kids_mode:
        tone_d = detent(getattr(_cfg, "PERSONA_TONE_KIDS", 0.5))
        persona = "kids"
    else:
        tone_d = detent(getattr(_cfg, "PERSONA_TONE_ADULT", 0.5))
        persona = "adult"
    engage_d = detent(getattr(_cfg, "PERSONA_ENGAGE", 0.5))
    return compose(persona, tone_d, engage_d)


# ── Offline selftest (no GPU, no network) ─────────────────────────────────────

def _selftest() -> bool:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    # 1. Detent snapping: exact stops and midpoints.
    check("0.5 -> 0 (neutral)", detent(0.5) == 0)
    check("0.0 -> -2", detent(0.0) == -2)
    check("1.0 -> +2", detent(1.0) == 2)
    check("0.25 -> -1", detent(0.25) == -1)
    check("0.75 -> +1", detent(0.75) == 1)
    check("0.6 snaps to nearest (0)", detent(0.6) == 0)
    check("0.7 snaps to nearest (+1)", detent(0.7) == 1)

    # 2. Garbage / out-of-range config values are neutral, never an extreme.
    for bad in (None, "", "high", -0.3, 1.7, [0.5]):
        check(f"bad value {bad!r} -> 0", detent(bad) == 0)

    # 3. Neutral composes EMPTY (the zero-change deploy guarantee).
    check("adult all-0 -> empty", compose("adult", 0, 0) == "")
    check("kids all-0 -> empty", compose("kids", 0, 0) == "")

    # 3b. S227: the conversation lean is GONE, not switchable. Nothing composed
    # from here may steer reply length or push a question back at the user --
    # that is the modelfile persona's job and the two contradicted each other.
    _all = " ".join(compose(p, t, e) for p in ("adult", "kids")
                    for t in DETENTS for e in DETENTS).lower()
    for _banned in ("kitchen table", "whole thought", "hand the turn back",
                    "past the first quip"):
        check(f"no length lean anywhere: {_banned!r}", _banned not in _all)
    check("compose() takes no convo argument",
          "convo" not in compose.__code__.co_varnames)

    # 4. Every non-zero detent composes, wrapped, under the cap.
    for persona in ("adult", "kids"):
        for t in DETENTS:
            for e in DETENTS:
                c = compose(persona, t, e)
                if t == 0 and e == 0:
                    continue
                check(f"{persona} tone={t:+d} engage={e:+d} composes",
                      c.startswith(_WRAP_OPEN) and c.endswith(")")
                      and len(c) <= _MAX_TUNING_CHARS)

    # 5. Name-blind: no fragment (any combination) contains a child's name.
    names = ("ava", "ben", "pip", "chip", "biscuit")
    worst = " ".join(list(_TONE_ADULT.values()) + list(_TONE_KIDS.values())
                     + list(_ENGAGE.values())).lower()
    for n in names:
        check(f"no name '{n}' in any fragment",
              n not in worst.replace("leopard", ""))

    # 6. Kids cheeky extremes carry the never-at-the-child invariant verbatim.
    for d in (1, 2):
        check(f"kids tone {d:+d} restates never-on-the-child",
              "never on the child" in _TONE_KIDS[d])

    # 7. Both axes present -> both fragments in one wrapper, single paren pair.
    c = compose("kids", 2, -2)
    check("combined clause single wrapper",
          c.count("(") == 1 and c.count(")") == 1
          and "cheekiest" in c and "listening mood" in c)

    # 8. tuning_clause reads config defaults (0.5 everywhere) -> empty.
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        clause = tuning_clause(True) + tuning_clause(False)
        check("tuning_clause at config defaults -> empty", clause == "")
    except Exception as e:
        check(f"tuning_clause import/exec failed: {e}", False)

    print(f"persona_tuning selftest: {'ALL PASS' if ok else 'FAILURES'}")
    return ok


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    print("usage: python core/persona_tuning.py --selftest")
