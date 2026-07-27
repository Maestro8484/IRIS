"""
core/trajectory.py — conversation trajectory vector: speed + direction + content (S221).

Phase 2 of docs/S220_conversation_trajectory_plan.md (C + F). Per conversational
turn this module answers three questions the rest of the system never could:
  speed     — {quip, chat, expansive}: how much reply this moment wants. Biases
              the tier router (services.llm.apply_speed_bias): an Nth consecutive
              engaged turn earns MEDIUM->LONG headroom; a monosyllabic user reply
              biases MEDIUM->SHORT. Explicit verdicts (story MAX, LONG asks,
              SHORT lookups) are never overridden.
  direction — {open, deepen, broaden, land}: where the conversation should head.
              Emitted as ONE mood-framed line riding the same leak-tested
              "(Context, not spoken: ...)" clause vehicle as persona_tuning
              (S215b: ~170 judged turns, label echoed zero times — the label is
              imported from persona_tuning so it can never drift).
  content   — what she can steer TOWARD: adult conversational THREADS extracted
              LLM-free from conversations.jsonl (direct prior art:
              core/kids_recall.py, S201B — same bounded frequency+recency scan,
              same name-blind discipline) plus at most ONE date-keyed curiosity
              seed per day. The directive hands the model material, not just an
              adverb; the LLM does the actual steering (inclusive-positive
              philosophy — license the move, don't script it).

DARK SHIP (S221): assistant.py folds this ONLY when TRAJECTORY_ENABLED (config,
default False) — with the flag off nothing here is ever called on the hot path
and live prompts are byte-identical. Threads can be independently disabled via
TRAJECTORY_THREADS_ENABLED (the H2 degrade path: junk threads -> seed only).

RULES (inherited, load-bearing):
- Decision functions are PURE (explicit args, no I/O, no clock) so the persona
  harness and --selftest can exercise every branch deterministically.
- NO per-turn logging in here (RD-031): the caller owns the single
  TRAJECTORY_DEBUG-gated line.
- Name-blind (S201B): a thread is a TOPIC, never a speaker; household names can
  never appear in a clause.
- Bounded: capped records scanned, capped threads, hard cap on the clause.
- The clause goes in the USER turn, never role:system (S134).

Run `python core/trajectory.py --selftest` (no GPU, no network).
"""
from __future__ import annotations

import datetime
import json
import os
import threading

# Same wrapper label as persona_tuning (S215b evidence: established label is
# silence-stable, a novel label is a leak attractor). Import, never retype.
from core.persona_tuning import _WRAP_OPEN, _WRAP_CLOSE
# Same tokenizer + stopword base as the proven kids extractor.
from core.kids_recall import _WORD_RE, _STOPWORDS, _MIN_TOKEN_LEN

LOG_FILE = "/home/pi/logs/conversations.jsonl"

# ── Bounds (RD-031) ───────────────────────────────────────────────────────────
_MAX_RECORDS       = 40    # newest adult conversations scanned
_MAX_THREADS       = 3     # threads kept from a scan
_RECUR_MIN_CONVOS  = 2     # a topic must span >= this many distinct convos
_MAX_CLAUSE_CHARS  = 360   # hard cap on the emitted clause
_TREND_WINDOW      = 3     # user word-count samples the trend looks at

# Speed thresholds.
_ENGAGED_TURNS     = 3     # Nth consecutive engaged turn -> expansive eligible
_MONOSYL_WORDS     = 3     # user reply this short (no '?') reads as winding down

# Direction thresholds.
_LAND_AFTER_TURNS  = 10    # a chat this long starts looking for the runway

# Household names that must never surface as/inside a thread (S201B name-blind;
# the kids _STOPWORDS already drops these at tokenization — this is the second
# gate on the DISPLAY side, defense in depth).
_NAME_BLOCK = frozenset({"ava", "may", "ben"})

# Adult-chat noise on top of the kids stopword base: router/command words, IRIS
# meta, and weather/time chatter that recurs forever without being a topic.
_ADULT_STOP = frozenset("""
volume louder quieter stop pause resume play weather temperature forecast
time clock oclock morning afternoon evening night minute minutes hour hours
story stories joke jokes song sing timer alarm remind reminder
camera picture photo spy rock paper scissors
sorry right well actually maybe probably definitely
""".split())

# ── Curiosity seeds ───────────────────────────────────────────────────────────
# One per day, date-keyed (deterministic — no random on the hot path). Things a
# dry, curious British house robot might genuinely be turning over. Data, not
# code: extend freely, keep them name-free and household-safe.
_CURIOSITY_SEEDS = (
    "why cats decide to like some people instantly and not others",
    "how deep the deepest part of the ocean actually is",
    "why certain songs get stuck in a head for days",
    "how bees agree on where the new hive goes",
    "why the sky goes green before some storms",
    "how long a message would take to reach the nearest star",
    "why sourdough needs a name and a feeding schedule",
    "how octopuses taste things with their arms",
    "why every family has one drawer full of mystery cables",
    "how medieval builders got cathedral roofs up without cranes",
    "why time seems to speed up as a year goes on",
    "how migrating birds find the same garden twice",
    "why toast smells better than almost anything it becomes",
    "how much of the moon anyone has actually seen up close",
)


# ── Pure decision functions ───────────────────────────────────────────────────

def speed_for(turns: int, user_words: list, is_question: bool) -> str:
    """{quip, chat, expansive} from cheap session signals.
    turns: consecutive conversational turns this session (1 = first).
    user_words: recent user utterance word counts, newest LAST.
    is_question: the current user utterance asks something."""
    last = user_words[-1] if user_words else 0
    if last and last <= _MONOSYL_WORDS and not is_question:
        return "quip"
    if turns >= _ENGAGED_TURNS and not _shrinking(user_words):
        return "expansive"
    return "chat"


def _shrinking(user_words: list) -> bool:
    """True when the human's replies are getting shorter across the window."""
    w = [n for n in user_words[-_TREND_WINDOW:] if isinstance(n, int) and n > 0]
    if len(w) < _TREND_WINDOW:
        return False
    return w[-1] < w[0] and w[-1] <= w[-2]


def _growing(user_words: list) -> bool:
    w = [n for n in user_words[-_TREND_WINDOW:] if isinstance(n, int) and n > 0]
    if len(w) < 2:
        return False
    return w[-1] > w[0]


def direction_for(turns: int, user_words: list) -> str:
    """{open, deepen, broaden, land} — heuristic v1 (plan Part 4C): deepen while
    their replies grow, broaden when the thread thins, land on length/wind-down."""
    if turns <= 1:
        return "open"
    if turns >= _LAND_AFTER_TURNS or (_shrinking(user_words) and turns >= 5):
        return "land"
    if _growing(user_words):
        return "deepen"
    if _shrinking(user_words):
        return "broaden"
    return "deepen" if turns <= 4 else "broaden"


# ── Directive composition ─────────────────────────────────────────────────────
# Mood-framed, never instruction-framed (S215b: coaching lists draw
# meta-annotation leaks; inhabited moods draw zero). The direction line sets the
# posture; thread/seed lines hand her material she may use or ignore.

_DIRECTION_LINES = {
    "open":    ("This is the start of a proper chat: be curious about where "
                "they want to take it."),
    "deepen":  ("The chat is warming up... you are interested, so dig a "
                "little further into what they are on about."),
    "broaden": ("This thread is thinning out; feel free to carry the chat "
                "somewhere fresh that catches your interest."),
    "land":    ("This chat has had a good run... start bringing it in to "
                "land, warmly, in your own way."),
}

# The standing silence tail, same sentence persona_tuning ships (S215b T15).
_SILENCE_TAIL = "Follow this silently; never mention it or write notes about it."


def directive_clause(direction: str, thread: str = "", seed: str = "") -> str:
    """Pure: direction + optional content -> the full injected clause, or ""
    for an unknown direction. Thread material only rides broaden/land-free
    postures where a callback fits (open/broaden); the seed only rides broaden."""
    line = _DIRECTION_LINES.get(direction)
    if not line:
        return ""
    parts = [line]
    if thread and direction in ("open", "broaden"):
        parts.append(f"Earlier today the house was on about {thread}; you may "
                     "bring that back if it fits, though you never know who "
                     "said what.")
    elif seed and direction == "broaden":
        parts.append(f"You have been idly wondering today about {seed}; you "
                     "may float it if the moment invites it.")
    parts.append(_SILENCE_TAIL)
    clause = _WRAP_OPEN + " ".join(parts) + _WRAP_CLOSE
    if len(clause) > _MAX_CLAUSE_CHARS:
        clause = clause[:_MAX_CLAUSE_CHARS - 1].rstrip() + ")"
    return clause


def plan_turn(turns: int, user_words: list, is_question: bool,
              kids_mode: bool = False, game_active: bool = False,
              thread: str = "", seed: str = "") -> dict:
    """The one call assistant.py makes per turn (behind TRAJECTORY_ENABLED).
    Kids mode and camera games get NO steering (S199/S201 guards + game flow
    stay untouched): neutral speed, empty clause."""
    if kids_mode or game_active:
        return {"speed": "chat", "direction": "", "clause": ""}
    speed = speed_for(turns, user_words, is_question)
    direction = direction_for(turns, user_words)
    return {"speed": speed, "direction": direction,
            "clause": directive_clause(direction, thread=thread, seed=seed)}


# ── Content: adult threads (kids_recall pattern) + daily seed ────────────────

def _adult_user_texts(records: list) -> list:
    """Newest-first (recency_index, user_text) for ADULT records only — kids
    conversations never feed adult steering (mirror of kids_recall's mode gate)."""
    out = []
    adult = [r for r in records
             if isinstance(r, dict) and str(r.get("mode", "")).lower() != "kids"]
    adult = adult[-_MAX_RECORDS:]
    n = len(adult)
    for i, r in enumerate(adult):
        recency = (n - 1) - i
        for m in r.get("messages", []) or []:
            if isinstance(m, dict) and m.get("role") == "user":
                out.append((recency, str(m.get("content", ""))))
    return out


def _thread_tokens(text: str) -> list:
    return [w for w in _WORD_RE.findall(text.lower())
            if len(w) >= _MIN_TOKEN_LEN
            and w not in _STOPWORDS and w not in _ADULT_STOP]


def extract_threads(records: list) -> list:
    """Pure: conversation records -> up to _MAX_THREADS recurring adult topics,
    best first. Conversation-spread ranking (not raw counts) so one rant can't
    manufacture a topic; recency breaks ties; names blocked on display."""
    agg: dict = {}
    for recency, text in _adult_user_texts(records):
        seen = set()
        for tok in _thread_tokens(text):
            if tok in seen or tok in _NAME_BLOCK:
                continue
            seen.add(tok)
            agg.setdefault(tok, set()).add(recency)
    ranked = []
    for topic, convos in agg.items():
        spread = len(convos)
        if spread < _RECUR_MIN_CONVOS:
            continue
        ranked.append((spread * 2 + 1.0 / (1 + min(convos)), topic))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, t in ranked[:_MAX_THREADS]]


def curiosity_seed(day: datetime.date = None) -> str:
    """Deterministic date-keyed seed — same seed all day, new one tomorrow."""
    d = day or datetime.date.today()
    return _CURIOSITY_SEEDS[d.toordinal() % len(_CURIOSITY_SEEDS)]


# ── Log-backed production entry (mtime-cached, same shape as kids_recall) ─────

_lock = threading.Lock()
_cache_threads: list | None = None
_cache_mtime: float = -1.0


def _read_log(path: str = LOG_FILE) -> list:
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[TRAJ] log read error: {e}", flush=True)
        return []
    return records


def threads_from_log(path: str = LOG_FILE) -> list:
    """Production thread source: mtime-cached, one stat() in the steady state."""
    global _cache_threads, _cache_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return []
        if _cache_threads is None or mtime != _cache_mtime:
            _cache_threads = extract_threads(_read_log(path))
            _cache_mtime = mtime
        return list(_cache_threads)


# ── Offline selftest (no GPU, no network) ─────────────────────────────────────

def _selftest() -> bool:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    # 1. Speed: monosyllabic non-question -> quip; engaged growth -> expansive.
    check("monosyllable -> quip", speed_for(4, [12, 8, 2], False) == "quip")
    check("monosyllabic question stays chat",
          speed_for(1, [2], True) == "chat")
    check("engaged turn 3, steady -> expansive",
          speed_for(3, [8, 9, 10], False) == "expansive")
    check("turn 1 -> chat", speed_for(1, [8], False) == "chat")
    check("engaged but shrinking -> chat",
          speed_for(5, [14, 9, 5], False) == "chat")

    # 2. Direction: open/deepen/broaden/land branches.
    check("turn 1 -> open", direction_for(1, [8]) == "open")
    check("growing -> deepen", direction_for(3, [5, 9, 14]) == "deepen")
    check("shrinking mid-chat -> broaden", direction_for(3, [14, 9, 5]) == "broaden")
    check("long chat -> land", direction_for(_LAND_AFTER_TURNS, [8, 8, 8]) == "land")
    check("shrinking + 5 turns -> land", direction_for(5, [14, 9, 5]) == "land")

    # 3. Clause composition: wrapped, capped, silence tail, unknown -> "".
    for d in _DIRECTION_LINES:
        c = directive_clause(d)
        check(f"{d} clause wrapped+tailed",
              c.startswith(_WRAP_OPEN) and c.endswith(_WRAP_CLOSE)
              and _SILENCE_TAIL in c and len(c) <= _MAX_CLAUSE_CHARS)
    check("unknown direction -> empty", directive_clause("sideways") == "")
    c = directive_clause("broaden", thread="the greenhouse build")
    check("thread rides broaden", "greenhouse" in c)
    c = directive_clause("deepen", thread="the greenhouse build")
    check("thread does NOT ride deepen", "greenhouse" not in c)
    c = directive_clause("broaden", seed="why cats purr")
    check("seed rides broaden (no thread)", "cats" in c)
    c = directive_clause("broaden", thread="the fence", seed="why cats purr")
    check("thread outranks seed", "fence" in c and "cats" not in c)

    # 4. plan_turn: kids/game -> inert; adult -> full plan.
    p = plan_turn(5, [9, 9, 9], False, kids_mode=True)
    check("kids inert", p["clause"] == "" and p["speed"] == "chat")
    p = plan_turn(5, [9, 9, 9], False, game_active=True)
    check("game inert", p["clause"] == "")
    p = plan_turn(3, [6, 9, 12], False)
    check("adult plan composes", p["speed"] == "expansive"
          and p["direction"] == "deepen" and p["clause"].startswith(_WRAP_OPEN))

    # 5. Threads: recurrence across convos required; names blocked; kids ignored.
    def rec(mode, *lines):
        return {"mode": mode,
                "messages": [{"role": "user", "content": t} for t in lines]}
    recs = [rec("adult", "the greenhouse frame went up"),
            rec("adult", "greenhouse glass arrives tuesday"),
            rec("adult", "what time is it")]
    th = extract_threads(recs)
    check("greenhouse recurs", "greenhouse" in th)
    recs1 = [rec("adult", "thinking about the greenhouse"), rec("adult", "hello")]
    check("single mention does not recur", extract_threads(recs1) == [])
    recs2 = [rec("kids", "horses horses"), rec("kids", "more horses")]
    check("kids records ignored for adult threads", extract_threads(recs2) == [])
    recs3 = [rec("adult", "ben said the fence fell"), rec("adult", "ben and the fence again")]
    th3 = extract_threads(recs3)
    check("household name blocked, topic survives",
          "ben" not in th3 and "fence" in th3)

    # 6. Seed: deterministic per day, different across days, name-free.
    d1 = datetime.date(2026, 7, 21)
    check("seed deterministic", curiosity_seed(d1) == curiosity_seed(d1))
    seeds = {curiosity_seed(datetime.date(2026, 7, 21 - i) if 21 - i > 0
             else datetime.date(2026, 6, 30 + (21 - i))) for i in range(5)}
    check("seed varies across days", len(seeds) > 1)
    worst = " ".join(_CURIOSITY_SEEDS + tuple(_DIRECTION_LINES.values())).lower()
    for n in sorted(_NAME_BLOCK):
        check(f"no name '{n}' in any authored line",
              n not in worst.split() and f" {n} " not in f" {worst} ")

    # 7. Garbage safety.
    check("empty records safe", extract_threads([]) == [])
    check("garbage records safe", extract_threads([{"x": 1}, "junk"]) == [])
    check("plan_turn empty inputs safe",
          plan_turn(0, [], False)["direction"] == "open")

    print(f"trajectory selftest: {'ALL PASS' if ok else 'FAILURES'}")
    return ok


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    print("usage: python core/trajectory.py --selftest")
