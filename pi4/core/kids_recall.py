"""
core/kids_recall.py — cheap, local, recurring-topic + recent-event recall for kids mode.

Reads the EXISTING conversation log (/home/pi/logs/conversations.jsonl, written by
assistant.flush_conversation_log) and surfaces 1-2 short "recall hooks" — context
cues that let iris-kids open a later chat with genuine continuity
("I remember someone was on about the swimming pool -- was that you?").

DESIGN (judged by the LENS):
- data > code, local > cloud, character lives in the modelfile.
- NO second LLM on the hot path. Pure token frequency + recency + a small curated
  child-interest lexicon. One mtime-cached file read per kids turn, nothing more.
  (S160b: any model touch on the conversational hot path must be benchmarked under
  concurrent GPU load first -- we add none.)
- name-BLIND (S201B / B2). iris has no speaker recognition, so a hook NEVER asserts
  WHO said it. Recall is surfaced as a shared-house memory the modelfile can attribute
  vaguely or turn into a question ("was that you?"), never "you, Ava, said X". The
  injected clause carries the DATA (what was talked about); the modelfile SYSTEM
  carries the PHRASING license (how to voice it honestly).
- bounded (RD-031): scans only the last _MAX_RECORDS kids conversations, returns at
  most _MAX_HOOKS short hooks, each length-capped. It can never blow up num_ctx (6144).

INJECTION: recall_clause() is appended to kids_profile.prompt_context(), which
assistant._build_messages() folds into the CURRENT USER TURN -- never as a
{"role":"system"} message (S134 regression). assistant.py is NOT edited; recall
rides the existing no-arg prompt_context() call.

The signal extractor is a PURE function of conversation records so the persona
harness can feed it scripted conversations and assert the callback deterministically.
Run `python core/kids_recall.py --selftest` (no GPU) to exercise it offline.
"""
from __future__ import annotations

import json
import os
import re
import threading

# Recall source: the same per-conversation log assistant.flush_conversation_log
# already writes. We only READ it (Session A owns keeping it written).
LOG_FILE = "/home/pi/logs/conversations.jsonl"

# ── Bounds (RD-031) ───────────────────────────────────────────────────────────
_MAX_RECORDS      = 40   # newest kids conversations scanned
_MAX_HOOKS        = 2    # hooks surfaced per turn (a couple, never a transcript)
_MAX_CLAUSE_CHARS = 240  # hard cap on the injected recall clause
_MIN_TOKEN_LEN    = 3    # ignore 1-2 char tokens
_RECUR_MIN_CONVOS = 2    # a topic must appear in >= this many DISTINCT convos to "recur"

# ── Stopwords: function words + kids-chat noise that must never become a hook ──
# Insults / meta / router words are here on purpose: "I remember you calling me
# boring" is not the recall we want. Interests and events are what survive.
_STOPWORDS = frozenset("""
a an the and or but so if then than that this these those there here
i you he she it we they me him her us them my your his its our their mine yours
is am are was were be been being do does did doing done have has had having
will would shall should can could may might must not no nor yes
of to in on at by for with from into onto over under up down out off about as
what when where who whom which why how whose
now today tonight day days time really very just too also again still even only
some any all each every both few more most other such own same
please thanks thank ok okay okey hey hello hi bye
know think want like love need get got go going gone come came make made say said
tell told ask asked see saw look looked hear heard feel felt let put take took
thing things stuff something anything nothing everything someone anyone everyone
good bad nice cool great best worst better worse fun funny silly weird
one two three lot lots bit little big small new old
iris robot dumb stupid boring worst hate shut idiot jerk mean loser
mommy mom mum daddy dad
ava may ben
""".split())

# ── Curated interest lexicon: keyword -> canonical display topic ───────────────
# Gives KNOWN family interests clean phrasing. Anything not here still surfaces via
# the generic content-noun path, so novel topics (tooth, puppy, party) are caught.
# Keep this in the family's own vocabulary; it is data, extend freely.
_INTEREST_LEXICON = {
    "horse": "horses", "horses": "horses", "pony": "horses", "ponies": "horses",
    "riding": "horse riding", "ride": "horse riding",
    "pool": "the swimming pool", "swim": "swimming", "swimming": "swimming",
    "swam": "swimming",
    "lacrosse": "lacrosse",
    "dog": "the dogs", "dogs": "the dogs", "puppy": "the dogs", "puppies": "the dogs",
    "pip": "Pip", "rusty": "Rusty", "biscuit": "Biscuit",
    "draw": "drawing", "drawing": "drawing", "drawings": "drawing", "drew": "drawing",
    "paint": "painting", "painting": "painting",
    "baby": "her babies", "babies": "her babies",
    "graphic": "graphic novels", "novel": "graphic novels", "novels": "graphic novels",
    "comic": "graphic novels", "comics": "graphic novels",
    "dogman": "Dog Man",
    "friend": "friends", "friends": "friends",
    "watch": "the mobile-watch",
    "school": "school", "teacher": "school",
    "lego": "Lego", "legos": "Lego",
    "minecraft": "Minecraft", "roblox": "Roblox", "game": "games", "games": "games",
    "book": "books", "books": "books", "reading": "reading", "read": "reading",
    "dinosaur": "dinosaurs", "dinosaurs": "dinosaurs", "dino": "dinosaurs",
    "space": "space", "planet": "space", "planets": "space", "rocket": "space",
    "birthday": "a birthday", "party": "a party",
    "soccer": "soccer", "football": "football", "goal": "scoring a goal",
    "tooth": "a lost tooth", "teeth": "teeth",
}

# ── Recent-event detection ────────────────────────────────────────────────────
# Temporal markers that make a mention feel like a fresh EVENT worth asking after.
_EVENT_TIME_RE = re.compile(
    r"\b(today|yesterday|this morning|this afternoon|last night|earlier|"
    r"at school|after school|this week|the other day|just now|tomorrow)\b", re.I)
# Achievement / happening cues -> canonical short event phrase.
_EVENT_CUES = [
    (re.compile(r"\bscored?\b.*\bgoal\b|\bgoal\b", re.I),      "scoring a goal"),
    # tolerate an adjective between verb and noun: "lost my FIRST tooth", "wobbly tooth"
    (re.compile(r"\b(lost|lose|losing|wobbly|wiggly|pulled)\b[^.!?]{0,20}\btooth\b", re.I), "losing a tooth"),
    (re.compile(r"\bwon\b", re.I),                             "winning something"),
    (re.compile(r"\blost\b(?!.*tooth)", re.I),                 "losing something"),
    (re.compile(r"\b(birthday|party)\b", re.I),               "a party"),
    (re.compile(r"\b(new )?(puppy|kitten|pet)\b", re.I),       "a new pet"),
    (re.compile(r"\btrip|holiday|vacation\b", re.I),           "a trip"),
    (re.compile(r"\brace|match|tournament|game\b", re.I),      "a game"),
    (re.compile(r"\bmade\b|\bbuilt\b", re.I),                  "something they made"),
]

# No apostrophe in the class on purpose: "you're" -> "you","re" (both dropped as a
# stopword / too-short), so contractions can never leak in as a recurring "topic".
_WORD_RE = re.compile(r"[a-z]+")

_lock = threading.Lock()
_cache_hooks: list | None = None
_cache_mtime: float = -1.0


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _tokens(text: str) -> list:
    if not isinstance(text, str):
        return []
    return [w for w in _WORD_RE.findall(text.lower())
            if len(w) >= _MIN_TOKEN_LEN and w not in _STOPWORDS]


def _kids_user_texts(records: list) -> list:
    """Newest-first list of (recency_index, user_text) for kids-mode records.
    recency_index 0 = most recent conversation. Non-kids records are skipped so a
    child never gets an adult conversation recalled at them."""
    out = []
    kids = [r for r in records
            if isinstance(r, dict) and str(r.get("mode", "")).lower() == "kids"]
    kids = kids[-_MAX_RECORDS:]
    n = len(kids)
    for i, r in enumerate(kids):
        recency = (n - 1) - i  # last element -> 0 (most recent)
        for m in r.get("messages", []) or []:
            if isinstance(m, dict) and m.get("role") == "user":
                out.append((recency, str(m.get("content", ""))))
    return out


def _recurring_topics(user_texts: list) -> list:
    """Topics that appear across DISTINCT conversations, ranked by spread + recency.
    Conversation-frequency (not raw token count) so one repetitive rant can't
    manufacture a 'recurring' topic. Returns canonical display phrases, best first."""
    # canonical_topic -> {convos:set(recency), best_recency:int}
    agg: dict = {}
    for recency, text in user_texts:
        seen_this_text = set()
        for tok in _tokens(text):
            topic = _INTEREST_LEXICON.get(tok)
            if topic is None:
                # generic content noun: only counts toward recurrence if it shows
                # up on its own across convos (handled by using the raw token as a
                # provisional topic keyed by token).
                topic = tok
            if topic in seen_this_text:
                continue
            seen_this_text.add(topic)
            slot = agg.setdefault(topic, {"convos": set(), "curated": tok in _INTEREST_LEXICON})
            slot["convos"].add(recency)
    ranked = []
    for topic, slot in agg.items():
        spread = len(slot["convos"])
        if spread < _RECUR_MIN_CONVOS:
            continue
        recency_bonus = 1.0 / (1 + min(slot["convos"]))  # more recent -> higher
        # curated interests outrank bare generic nouns at equal spread
        score = spread * 2 + recency_bonus + (0.5 if slot["curated"] else 0.0)
        ranked.append((score, topic))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, t in ranked]


def _recent_event(user_texts: list):
    """The single freshest event-shaped mention (from the most recent convos), or
    None. Prefers a line carrying BOTH a temporal marker and an event cue; falls
    back to the most recent event cue alone."""
    best = None  # (recency, has_time, phrase)
    for recency, text in user_texts:
        has_time = bool(_EVENT_TIME_RE.search(text))
        phrase = None
        for rx, canon in _EVENT_CUES:
            if rx.search(text):
                phrase = canon
                break
        if phrase is None:
            continue
        cand = (recency, has_time, phrase)
        # rank: smaller recency first, then time-marked first
        if best is None or (recency, not has_time) < (best[0], not best[1]):
            best = cand
    return best[2] if best else None


# ── Public API ────────────────────────────────────────────────────────────────

def recall_signals(records: list) -> dict:
    """Pure: conversation records -> {'event': str|None, 'topics': [str,...]}.
    Harness-testable. `records` is a list of dicts shaped like conversations.jsonl
    lines ({mode, messages:[{role,content}...]})."""
    if not isinstance(records, list) or not records:
        return {"event": None, "topics": []}
    texts = _kids_user_texts(records)
    return {"event": _recent_event(texts), "topics": _recurring_topics(texts)}


def recall_hooks(records: list, max_hooks: int = _MAX_HOOKS) -> list:
    """Up to max_hooks short recall phrases, freshest/most-salient first. Prefers
    one recent EVENT (great for 'how did that go?') plus one recurring INTEREST."""
    sig = recall_signals(records)
    hooks = []
    if sig["event"]:
        hooks.append(sig["event"])
    for topic in sig["topics"]:
        if len(hooks) >= max_hooks:
            break
        # don't duplicate the event's subject as a topic
        if sig["event"] and topic.split()[-1] in sig["event"]:
            continue
        hooks.append(topic)
    return hooks[:max_hooks]


def recall_clause(records: list) -> str:
    """The injected DATA clause (or "" when there is nothing worth recalling).
    Phrasing/behaviour lives in the modelfile; this is just what was talked about,
    plus a terse name-blind reminder. Folded into the USER turn by _build_messages
    via kids_profile.prompt_context()."""
    hooks = recall_hooks(records)
    if not hooks:
        return ""
    body = "; ".join(hooks)
    clause = (f"(Recall from Dad's notebook, not spoken: lately the children have "
              f"been on about {body}. You may bring ONE up as your own memory, but "
              f"you do not know which child is speaking -- never attach a name.)")
    if len(clause) > _MAX_CLAUSE_CHARS:
        clause = clause[:_MAX_CLAUSE_CHARS - 1].rstrip() + ")"
    return clause


# ── Log-backed entry point (production) ───────────────────────────────────────

def _read_log(path: str = LOG_FILE) -> list:
    """Parse conversations.jsonl -> list of records. Tolerant: skips bad lines,
    returns [] if the file is absent. Never raises."""
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
        print(f"[KIDRECALL] log read error: {e}", flush=True)
        return []
    return records


def clause_from_log(path: str = LOG_FILE) -> str:
    """Production entry: mtime-cached recall clause from the live log. Called (via
    kids_profile.prompt_context) on each kids turn; the log only changes between
    conversations, so the cache makes the steady state a single stat()."""
    global _cache_hooks, _cache_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return ""
        if _cache_hooks is None or mtime != _cache_mtime:
            _cache_hooks = recall_clause(_read_log(path))
            _cache_mtime = mtime
        return _cache_hooks


# ── Offline selftest (no GPU) ─────────────────────────────────────────────────

def _selftest() -> bool:
    def rec(mode, *user_lines):
        msgs = []
        for u in user_lines:
            msgs.append({"role": "user", "content": u})
            msgs.append({"role": "assistant", "content": "ok"})
        return {"mode": mode, "messages": msgs}

    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    # 1. Recent event surfaces (tooth, with a temporal marker + intervening adjective).
    recs = [rec("kids", "I lost my first tooth at school today!"),
            rec("kids", "what are you")]
    sig = recall_signals(recs)
    check("recent tooth event detected", sig["event"] == "losing a tooth")
    check("tooth clause mentions tooth", "tooth" in recall_clause(recs).lower())

    # 2. Recurring interest across >=2 convos surfaces; single mention does not.
    recs = [rec("kids", "I rode my horse"), rec("kids", "horses are the best"),
            rec("kids", "tell me a joke")]
    topics = recall_signals(recs)["topics"]
    check("horses recur across 2 convos", "horses" in topics)
    recs1 = [rec("kids", "I like lacrosse"), rec("kids", "hi")]
    check("single mention does NOT recur", "lacrosse" not in recall_signals(recs1)["topics"])

    # 3. Name-blind: even when a child names themselves, the clause carries no name.
    recs = [rec("kids", "Ava loves the pool"), rec("kids", "we went to the pool")]
    clause = recall_clause(recs)
    check("clause produced from recurring pool", clause != "")
    check("clause is name-blind", "ava" not in clause.lower() and "ben" not in clause.lower())
    check("clause carries no-name reminder", "never attach a name" in clause.lower())

    # 4. Adult records are ignored for kids recall.
    recs = [rec("adult", "I lost a tooth today"), rec("adult", "teeth teeth teeth")]
    check("adult records ignored", recall_clause(recs) == "")

    # 5. Insults/meta never become a hook.
    recs = [rec("kids", "you're dumb"), rec("kids", "you're boring"),
            rec("kids", "stupid robot")]
    check("insults produce no recall", recall_clause(recs) == "")

    # 6. Bounded: at most _MAX_HOOKS.
    recs = [rec("kids", "horses horses"), rec("kids", "horses and the pool"),
            rec("kids", "the pool and lacrosse"), rec("kids", "lacrosse and dogs"),
            rec("kids", "dogs and drawing today")]
    check("hooks capped at _MAX_HOOKS", len(recall_hooks(recs)) <= _MAX_HOOKS)

    # 7. Empty / garbage input is safe.
    check("empty records -> empty clause", recall_clause([]) == "")
    check("garbage records -> empty clause", recall_clause([{"x": 1}, "junk"]) == "")

    print(f"kids_recall selftest: {'ALL PASS' if ok else 'FAILURES'}")
    return ok


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    print("usage: python kids_recall.py --selftest")
