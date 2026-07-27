"""
core/camera_games.py - Pure game logic for the camera games (S210).

No I/O, no network, no audio: parsers, judges, clue building, winner logic and
spoken-line templates for I Spy and Rock Paper Scissors. assistant.py owns
capture/TTS/state; services/vision.py owns the network calls.

Design principle (operator, S210): "confidence via vagueness" -- the vision
model's specific object labels are error-prone, so IRIS commits only to coarse
attributes she can be confident about (color, shape, location), delivers them
WITH confidence, and judges guesses generously at that same attribute level.
The LLM never judges a game: verdicts are decided here, in code, so IRIS can
never be wrong about her own answer and kids get to win.

Run `python3 -m core.camera_games --selftest` for the pure-logic checks.
"""

import difflib
import json
import random
import re

# ── Intent helpers (follow-up loop routing) ───────────────────────────────────
# Order matters at the call site: check is_negative() BEFORE is_affirmative()
# ("no more" contains "more"); check is_giveup() before judging a guess.

_AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|okay|ok|again|another|rematch|more|absolutely|"
    r"definitely|go on|let'?s go|play|one more)\b")
_NEGATIVE_RE = re.compile(
    r"\b(no|nope|nah|done|stop|quit|finished|enough|not now|later|bye|goodbye)\b")
_GIVEUP_RE = re.compile(
    r"\b(give up|i don'?t know|dunno|no idea|no clue|tell me|what is it|"
    r"i can'?t guess|i can'?t find|too hard|show me the answer)\b")


def is_affirmative(text: str) -> bool:
    return bool(_AFFIRM_RE.search(text.lower()))


def is_negative(text: str) -> bool:
    return bool(_NEGATIVE_RE.search(text.lower()))


def is_giveup(text: str) -> bool:
    return bool(_GIVEUP_RE.search(text.lower()))


# ── I Spy: pick parsing ───────────────────────────────────────────────────────

def parse_ispy_pick(raw: str) -> dict | None:
    """Extract and validate the structured I Spy pick from a model reply.
    Tolerates persona preamble, [EMOTION:X] tags and code fences: takes the
    first {...} blob, strict json first, then a trailing-comma cleanup pass.
    Returns {"object","synonyms","color","shape","location"} (strings
    lowercased, synonyms deduped and capped) or None if unusable."""
    m = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not m:
        return None
    blob = m.group(0)
    d = None
    for candidate in (blob, re.sub(r",\s*([}\]])", r"\1", blob)):
        try:
            d = json.loads(candidate)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(d, dict):
        return None
    obj = str(d.get("object", "") or "").strip().lower()
    if not (1 <= len(obj) <= 40):
        return None
    syns = d.get("synonyms", [])
    if not isinstance(syns, list):
        syns = []
    clean_syns = []
    for s in syns:
        s = str(s).strip().lower()
        if s and s != obj and s not in clean_syns and len(s) <= 40:
            clean_syns.append(s)
    return {
        "object":   obj,
        "synonyms": clean_syns[:6],
        "color":    str(d.get("color", "") or "").strip().lower()[:30],
        "shape":    str(d.get("shape", "") or "").strip().lower()[:40],
        "location": str(d.get("location", "") or "").strip().lower()[:60],
    }


_COLOR_WORDS = (
    "red", "blue", "green", "yellow", "white", "black", "brown", "orange",
    "purple", "pink", "grey", "gray", "silver", "gold", "tan", "beige",
    "cream", "turquoise", "maroon", "navy",
)
_SHAPE_ADJS = {
    "round", "square", "rectangular", "tall", "big", "small", "tiny", "long",
    "short", "flat", "curved", "pointy", "wide", "thin", "huge", "little",
    "boxy", "circular", "oval", "chunky", "skinny",
}
# The model narrates the PHOTO; the kid lives in the ROOM. Scrub camera-speak
# out of location clues (live catch S210: "center of the image").
_CAMERA_SPEAK_RE = re.compile(r"\b(?:the\s+)?(?:image|photo|picture|shot|scene|foreground|background)\b")


def build_ispy_clues(pick: dict) -> list:
    """Ordered vague -> specific clue fragments, each completing the sentence
    'I spy something ...'. Color first (the classic), then shape/size, then
    location. The model's attribute strings are messy in practice (live S210:
    color 'white frame and clear glass'), so each is sanitized in code:
    color -> first recognized color word; shape -> plain adjective(s) or a
    'shaped like a ...' phrase; location -> camera-speak scrubbed + spatial
    preposition. Always returns at least one clue."""
    clues = []
    for w in re.findall(r"[a-z]+", pick.get("color", "")):
        if w in _COLOR_WORDS:
            clues.append(w)
            break
    shape = pick.get("shape", "").strip()
    if shape:
        # leading adjectives are used plain ("something tall"), which is also
        # the vaguer clue; only noun-led phrases get the "shaped like a" wrap.
        words = shape.split()
        lead = []
        for w in words[:2]:
            if w not in _SHAPE_ADJS:
                break
            lead.append(w)
        if lead:
            clues.append(" ".join(lead))
        else:
            clues.append("shaped like a " + " ".join(words[:6]).rstrip(",;"))
    loc = _CAMERA_SPEAK_RE.sub("the room", pick.get("location", "")).strip()
    loc = re.sub(r"\bthe the\b", "the", re.sub(r"\s+", " ", loc))
    if loc:
        if not re.match(r"^(on|in|near|by|under|next to|behind|above|at|against)\b", loc):
            loc = "near the " + re.sub(r"^the\s+", "", loc)
        clues.append(loc)
    if not clues:
        clues.append("that I can see very clearly")
    return clues[:3]


# ── I Spy: guess judging (generous by design) ─────────────────────────────────

_GUESS_STOPWORDS = {
    "is", "it", "its", "a", "an", "the", "um", "uh", "maybe", "that", "this",
    "your", "my", "you", "i", "spy", "see", "something", "like", "think",
    "there", "then", "well", "hmm", "oh",
}


def _guess_words(s: str) -> list:
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
            if w not in _GUESS_STOPWORDS and len(w) > 1]


def _stem(w: str) -> str:
    if w.endswith("es") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and len(w) > 3:
        return w[:-1]
    return w


def judge_ispy_guess(guess: str, secret: str, synonyms: list) -> bool:
    """True if the child's guess plausibly names the secret object. Generous:
    whole-phrase containment, per-word stem equality, and close-spelling fuzzy
    (difflib >= 0.8) against the object name and every synonym. A guess of
    'bottle' matches 'water bottle'; 'cup' matches synonym 'mug' only via the
    synonyms list (the vision pick is asked to supply kid words)."""
    g_norm = " ".join(_guess_words(guess))
    if not g_norm:
        return False
    targets = [secret] + list(synonyms or [])
    g_words = [_stem(w) for w in g_norm.split()]
    for t in targets:
        t_norm = " ".join(_guess_words(t)) or t.strip().lower()
        if not t_norm:
            continue
        if len(t_norm) > 3 and (t_norm in g_norm or g_norm in t_norm):
            return True
        t_words = [_stem(w) for w in t_norm.split()]
        for gw in g_words:
            for tw in t_words:
                if gw == tw:
                    return True
                if len(gw) > 3 and len(tw) > 3 and \
                        difflib.SequenceMatcher(None, gw, tw).ratio() >= 0.8:
                    return True
    return False


# ── I Spy: spoken lines (kids + adult registers) ──────────────────────────────
# Every line ends handing the turn back (question or invite) so the follow-up
# loop stays alive. TTS notes: ellipses pause, no asterisks, no em dashes.

def ispy_start_line(clue: str, kids: bool) -> str:
    if kids:
        return random.choice([
            f"Let's play I Spy! I'm the spy, so I've secretly picked something I can see right now. "
            f"You're the finder... try to guess it! I spy with my little eye, something {clue}. What do you think it is?",
            f"Ooh, I Spy! Here's how it works. I'm the spy and I've picked something I can see. "
            f"You're the finder! I spy with my little eye, something {clue}. Have a guess!",
        ])
    return random.choice([
        f"Right, I Spy it is. I'm the spy... I've already picked something I can see, and your job is to find it. "
        f"I spy with my little eye, something {clue}. Go on then.",
        f"I Spy. I've picked my object, no take-backs. I spy with my little eye, something {clue}. Your guess?",
    ])


def ispy_wrong_line(next_clue: str, kids: bool) -> str:
    if kids:
        return random.choice([
            f"Ooh, good guess, but that's not it! Here's another clue... it's something {next_clue}. Try again!",
            f"So close! Not quite though. Next clue... I spy something {next_clue}. What could it be?",
        ])
    return random.choice([
        f"Nope. Another clue then... it's something {next_clue}. Try again.",
        f"Not that. Here's a hint... something {next_clue}. Go again.",
    ])


def ispy_correct_line(secret: str, kids: bool) -> str:
    if kids:
        return random.choice([
            f"YES! You found it! It was the {secret}! You're a brilliant finder! Want to spy another one?",
            f"You got it! The {secret}! Amazing finding! Shall we play again?",
        ])
    return random.choice([
        f"Got it in one... it was the {secret}. Well found. Another round?",
        f"Correct, the {secret}. Not bad at all. Again?",
    ])


def ispy_reveal_line(secret: str, kids: bool) -> str:
    if kids:
        return random.choice([
            f"Ooh, you were SO close! It was the {secret}! You nearly had it! Want to spy another one?",
            f"That was a tricky one! It was the {secret}. Great guessing though! Another go?",
        ])
    return random.choice([
        f"I'll put you out of your misery... it was the {secret}. Close though. Another round?",
        f"It was the {secret}. You were circling it. Again?",
    ])


def game_signoff_line(kids: bool) -> str:
    if kids:
        return random.choice([
            "Good game! Come find me when you want to play again!",
            "That was fun! Ask me any time you want another game!",
        ])
    return random.choice([
        "Good game. I'll be here.",
        "Fair enough. You know where to find me.",
    ])


# ── Rock Paper Scissors ───────────────────────────────────────────────────────

RPS_MOVES = ("rock", "paper", "scissors")

_RPS_WORD_RE = {
    "rock":     re.compile(r"\bROCK\b"),
    "paper":    re.compile(r"\bPAPER\b"),
    "scissors": re.compile(r"\bSCISSORS?\b"),
}


def rps_pick_move() -> str:
    return random.choice(RPS_MOVES)


def parse_rps_reply(raw: str) -> str:
    """One-word hand classification out of a possibly chatty persona reply.
    Exactly one distinct move mentioned -> that move; zero or several ->
    'unclear' (the round retries once, then skips gracefully)."""
    up = raw.upper()
    found = [m for m, rx in _RPS_WORD_RE.items() if rx.search(up)]
    return found[0] if len(found) == 1 else "unclear"


def rps_winner(kid: str, iris: str) -> str:
    """'kid' | 'iris' | 'tie'."""
    if kid == iris:
        return "tie"
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    return "kid" if beats[kid] == iris else "iris"


_RPS_BEATS_PHRASE = {
    ("rock", "scissors"):  "rock blunts scissors",
    ("paper", "rock"):     "paper wraps rock",
    ("scissors", "paper"): "scissors snip paper",
}


# S218: the throw is NO LONGER one utterance. S216 spelled the beats out as
# six-dot ellipses inside a single line and slowed synthesis via
# GAME_COUNTDOWN_SPEED; the operator's play-test found the result audibly
# UNEVEN, with a long hang specifically between "paper" and "scissors". That is
# the ceiling of the technique: an ellipsis is the only pause lever Kokoro
# honours (a comma is swallowed, an em dash does nothing -- S216), but its
# duration is not specified anywhere and is not consistent beat to beat, so
# punctuation cannot be used as a metronome.
#
# Each beat is now synthesised separately, trimmed of Kokoro's own (variable)
# padding, and laid out on a fixed onset PERIOD in assistant._rps_throw_pcm --
# silence measured in samples, which is a metronome in a way punctuation is not.
# Tunable live via RPS_BEAT_PERIOD_S; free at play time (the assembled throw is
# cached PCM).
#
# "Shoot!" is deliberately not "SHOOT!": as a standalone utterance an all-caps
# token risks being spelled out letter by letter, which is the same
# grapheme-mangling class as the S218 "Let's seeee" defect. It was only ever
# safe inside a longer sentence.
RPS_THROW_BEATS = ("Rock.", "Paper.", "Scissors.", "Shoot!")


def rps_countdown_line(first: bool, kids: bool) -> str:
    """The spoken lead-in ONLY. The throw beats follow as separate utterances
    (RPS_THROW_BEATS), so no countdown line may contain them."""
    if first:
        if kids:
            return ("Rock, paper, scissors! You're on! I've already locked in my move, so I can't cheat. "
                    "Hold your hand about an arm's length away, right in the middle of my view, "
                    "and keep it very still when I say shoot. "
                    "Ready?")
        return ("Rock paper scissors, then. My move's already locked in, so no funny business. "
                "Hold your hand about an arm's length from my camera, centred in my view, "
                "and keep it still after the shoot. "
                "Ready?")
    return random.choice([
        "Next round! Ready?",
        "Here we go again. Ready?",
    ])


def rps_unclear_line(kids: bool) -> str:
    if kids:
        return ("Oops, I couldn't quite see your hand! Hold it about an arm's length away, "
                "right in the middle of my view, and keep it nice and still. "
                "One more time!")
    return ("I didn't catch your hand there. Hold it about an arm's length away, centred in my view, "
            "and keep it still. "
            "Once more!")


def rps_skip_line(kids: bool, continuing: bool = False) -> str:
    """`continuing` (S216): the caller is rolling straight into the next round,
    so do not ask a question the player is not meant to answer."""
    if continuing:
        return ("My eyes missed that one! Mystery round." if kids
                else "My eyes missed that one. Mystery round.")
    if kids:
        return "My eyes missed it that time! Let's call that one a mystery round. Want to try another?"
    return "My eyes missed that one entirely. We'll strike that round from the record. Another?"


def rps_result_line(kid_move: str, iris_move: str, winner: str,
                    kid_score: int, iris_score: int, kids: bool,
                    auto_continue: bool = False) -> tuple:
    """Reveal both moves + verdict + running score. Returns
    (line, emotion, match_over). First to 2 takes the best-of-3 match.

    S216 tempo: with `auto_continue` the caller rolls straight into the next
    countdown, so a mid-match line ends FLAT (no "Again?") and the reveal and
    score are trimmed. Measured on the live 2026-07-20 match, asking a question
    the player was only ever going to answer with "again" cost ~5 s of listen
    plus endpoint EVERY round, on top of a 5.1 s result line, for ~1 s of play.
    Match-over lines always ask: there the turn genuinely does go back."""
    reveal = f"You showed {kid_move}, I picked {iris_move}."
    match_over = kid_score >= 2 or iris_score >= 2
    # S218: the comma here was doing the pacing work and Kokoro swallows commas,
    # so the tally came out as one rapid blurt ("you-one-me-zero"). An ellipsis
    # is the pause lever that survives synthesis (same finding as the throw
    # beats above), and it is the only thing separating two bare digits.
    score = f"You {kid_score}... me {iris_score}."
    tail = "" if auto_continue else " Again?"
    if winner == "tie":
        line = reveal + " " + random.choice([
            "Great minds! A tie, no points.",
            "Snap! A tie, no points.",
        ]) + tail
        return line, "AMUSED", False
    if winner == "kid":
        phrase = _RPS_BEATS_PHRASE[(kid_move, iris_move)].capitalize()
        if match_over:
            if kids:
                line = f"{reveal} {phrase}... you win the round, AND the match! You're the champion! Rematch?"
            else:
                line = f"{reveal} {phrase}... that's the match to you. I demand a rematch. Well, request one. Rematch?"
            return line, "SURPRISED", True
        line = f"{reveal} {phrase}... you win this round! {score}{tail}"
        return line, "SURPRISED", False
    phrase = _RPS_BEATS_PHRASE[(iris_move, kid_move)].capitalize()
    if match_over:
        if kids:
            line = f"{reveal} {phrase}... I win the match! But you nearly had me! Rematch?"
        else:
            line = f"{reveal} {phrase}... the match is mine. I'll try to be gracious about it. Rematch?"
        return line, "HAPPY", True
    line = f"{reveal} {phrase}... that round's mine! {score}{tail}"
    return line, "HAPPY", False


# ── Selftest ──────────────────────────────────────────────────────────────────

def _selftest() -> int:
    fails = 0

    def check(name, cond):
        nonlocal fails
        if not cond:
            fails += 1
            print(f"FAIL: {name}")
        else:
            print(f"ok:   {name}")

    # parse_ispy_pick
    p = parse_ispy_pick('[EMOTION:HAPPY] Here you go!\n```json\n'
                        '{"object": "Ball", "synonyms": ["toy ball", "sphere"], '
                        '"color": "Red", "shape": "round", "location": "the sofa"}\n```')
    check("pick parses through persona+fence", p is not None and p["object"] == "ball")
    check("pick lowercases + keeps synonyms", p["synonyms"] == ["toy ball", "sphere"])
    check("pick trailing-comma cleanup",
          parse_ispy_pick('{"object": "mug", "color": "blue",}') is not None)
    check("pick rejects no-object", parse_ispy_pick('{"color": "blue"}') is None)
    check("pick rejects no-json", parse_ispy_pick("I spy something blue!") is None)

    # build_ispy_clues
    c = build_ispy_clues(p)
    check("clues ordered color,shape,location", c == ["red", "round", "near the sofa"])
    check("clues bare-location normalized", build_ispy_clues(
        {"location": "on the table"})[0] == "on the table")
    check("clues never empty", build_ispy_clues({}) != [])
    # live-caught messy attributes (S210): phrase color, noun shape, camera-speak
    messy = build_ispy_clues({"color": "white frame and clear glass",
                              "shape": "tall rectangle with a pointy top",
                              "location": "center of the image"})
    check("clue color sanitized to word", messy[0] == "white")
    check("clue leading-adjective shape plain", messy[1] == "tall")
    check("clue camera-speak scrubbed", "image" not in messy[2] and messy[2].startswith(("near", "in")))
    check("clue adj-pair shape plain", build_ispy_clues({"shape": "tall thin"})[0] == "tall thin")
    check("clue adj-word shape plain", build_ispy_clues(
        {"shape": "rectangular with a triangular top"})[0] == "rectangular")
    check("clue noun shape wrapped", build_ispy_clues(
        {"shape": "rectangle with a pointy top"})[0] == "shaped like a rectangle with a pointy top")

    # judge_ispy_guess
    check("judge exact", judge_ispy_guess("a ball", "ball", []))
    check("judge article+phrase", judge_ispy_guess("is it the ball?", "ball", []))
    check("judge plural", judge_ispy_guess("balls", "ball", []))
    check("judge synonym", judge_ispy_guess("the mug", "cup", ["mug", "coffee cup"]))
    check("judge multiword secret", judge_ispy_guess("the bottle", "water bottle", []))
    check("judge fuzzy spelling", judge_ispy_guess("the bottel", "bottle", []))
    check("judge rejects wrong", not judge_ispy_guess("banana", "ball", ["toy ball"]))
    check("judge rejects empty", not judge_ispy_guess("um the", "ball", []))
    check("judge rejects stopword-collision", not judge_ispy_guess("i spy", "spyglass", []))

    # intents
    check("affirm yes", is_affirmative("yes please"))
    check("affirm again", is_affirmative("play again!"))
    check("negative no", is_negative("no thanks"))
    check("negative no-more (order dep)", is_negative("no more"))
    check("giveup", is_giveup("I don't know, tell me"))
    check("giveup not on guess", not is_giveup("is it the lamp"))

    # RPS
    check("rps parse clean", parse_rps_reply("ROCK") == "rock")
    check("rps parse chatty", parse_rps_reply("[EMOTION:HAPPY] Ooh! That's SCISSORS!") == "scissors")
    check("rps parse singular scissor", parse_rps_reply("a scissor gesture") == "scissors")
    check("rps parse multiple->unclear", parse_rps_reply("rock or maybe paper") == "unclear")
    check("rps parse none->unclear", parse_rps_reply("I see a cat") == "unclear")
    check("rps winner table", rps_winner("rock", "scissors") == "kid"
          and rps_winner("rock", "paper") == "iris" and rps_winner("rock", "rock") == "tie"
          and rps_winner("paper", "rock") == "kid" and rps_winner("scissors", "paper") == "kid")

    # result lines
    line, emo, over = rps_result_line("rock", "scissors", "kid", 2, 0, kids=True)
    check("rps match-over kid", over and "match" in line.lower() and emo == "SURPRISED")
    line, emo, over = rps_result_line("rock", "rock", "tie", 1, 1, kids=False)
    check("rps tie", not over and "tie" in line.lower())
    line, emo, over = rps_result_line("paper", "scissors", "iris", 0, 1, kids=True)
    check("rps iris round", not over and "mine" in line.lower())
    # S216 auto-continue: mid-match lines must NOT ask a question (the caller
    # rolls straight on), but a match-ending line always hands the turn back.
    mid, _, mid_over = rps_result_line("rock", "paper", "iris", 0, 1, kids=True,
                                       auto_continue=True)
    check("rps auto-continue mid-match asks nothing",
          not mid_over and not mid.rstrip().endswith("?") and "again" not in mid.lower())
    end, _, end_over = rps_result_line("rock", "paper", "iris", 0, 2, kids=True,
                                       auto_continue=True)
    check("rps auto-continue match end still asks",
          end_over and end.rstrip().endswith("?"))
    check("rps skip continuing asks nothing",
          not rps_skip_line(True, continuing=True).rstrip().endswith("?"))
    check("rps skip default still asks", rps_skip_line(True).rstrip().endswith("?"))
    # S218: the throw is played as separate cached beats with real gaps, so NO
    # countdown line may still carry it inline -- a line that does would speak
    # the throw twice, once fast and once in rhythm.
    _cd_lines = [rps_countdown_line(True, True), rps_countdown_line(True, False),
                 rps_countdown_line(False, False), rps_unclear_line(True),
                 rps_unclear_line(False)]
    check("countdown carries no inline throw",
          not any("shoot!" in ln.lower() or "......" in ln for ln in _cd_lines))
    check("throw beats are 4 short utterances",
          len(RPS_THROW_BEATS) == 4 and RPS_THROW_BEATS[-1] == "Shoot!"
          and all(0 < len(b) <= 12 for b in RPS_THROW_BEATS))
    check("throw beats not shouted (grapheme-mangle guard)",
          not any(b.isupper() for b in RPS_THROW_BEATS))
    # S218 tally: the score must not lean on a comma, which Kokoro swallows.
    _sl = rps_result_line("rock", "scissors", "kid", 1, 0, kids=False)[0]
    check("tally paced by ellipsis not comma", "You 1... me 0." in _sl)

    # every spoken line hands the turn back (ends with ? or !)
    for name, ln in [
        ("start", ispy_start_line("red", True)), ("wrong", ispy_wrong_line("round", True)),
        ("correct", ispy_correct_line("ball", True)), ("reveal", ispy_reveal_line("ball", False)),
        ("skip", rps_skip_line(True)),
    ]:
        check(f"line '{name}' hands turn back", ln.rstrip().endswith(("?", "!")))
    # S218: the lead-in now hands off to the beats, so it ends on a cue, not on
    # the throw. Still must end on a spoken hand-off, never mid-thought.
    for ln in [rps_countdown_line(True, True), rps_countdown_line(True, False),
               rps_countdown_line(False, False), rps_unclear_line(True),
               rps_unclear_line(False)]:
        check("countdown lead-in cues the throw", ln.rstrip().endswith(("?", "!")))
    # no em dashes in any spoken template (operator rule + TTS). chr() so this
    # check can't trip on its own source.
    _em = chr(0x2014)
    spoken = [ispy_start_line("red", k) for k in (True, False)] + \
             [ispy_wrong_line("round", k) for k in (True, False)] + \
             [ispy_correct_line("ball", k) for k in (True, False)] + \
             [ispy_reveal_line("ball", k) for k in (True, False)] + \
             [game_signoff_line(k) for k in (True, False)] + \
             [rps_countdown_line(f, k) for f in (True, False) for k in (True, False)] + \
             [rps_unclear_line(k) for k in (True, False)] + \
             [rps_skip_line(k) for k in (True, False)] + \
             [rps_result_line(a, b, rps_winner(a, b), s, 3 - s, k)[0]
              for a in RPS_MOVES for b in RPS_MOVES for s in (0, 2) for k in (True, False)]
    check("no em dashes in spoken lines", not any(_em in ln for ln in spoken))

    print(f"\n{'PASS' if fails == 0 else 'FAIL'} ({fails} failures)")
    return fails


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
