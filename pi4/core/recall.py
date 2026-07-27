"""
core/recall.py - episodic recall: "hey iris, how did that unicorn story end?"

Phase D of docs/handoffs/S224_episodic_recall_brief.md. The store, the typed
episodes and the embedding index all live on GandalfAI (scripts/iris_episodes.py,
scripts/iris_corpus_server.py, port 8006). This module is the Pi's whole share of
the feature: notice a recall question, ask for the memory, and hand it to her.

WHY THERE IS NO PI-SIDE FALLBACK: answering any recall question needs the LLM,
which is on GandalfAI. There is no state where she is asked to remember something,
cannot reach him, and could still answer. A local index would be code for a state
that cannot occur (brief Part 0).

THE CLAUSE RIDES THE USER TURN. NEVER a {"role":"system"} message: in Ollama
/api/chat a request system message REPLACES the modelfile SYSTEM and strips the
persona. That is the S134 regression, which recurred live at S197. Recall uses the
same vehicle as kids_profile and persona_tuning - the "(Context, not spoken: ...)"
stamp assistant._build_messages() folds into the CURRENT user turn.

SHE HAS NO SPEAKER RECOGNITION, so the clause never says WHO said anything. It
says "someone in the house", in adult mode as well as kids, because that is simply
true - the roster was removed at S216 and asserting a name was the S201B
wrong-child root cause. The modelfile carries the phrasing license ("was that
you?"); this module carries only the data.

THE MISS PATH IS THE DEFAULT. A confident wrong memory is worse than "I don't
remember that one", and a child hits the miss far more often than the hit. So:
retrieval below RECALL_MIN_SCORE injects NOTHING, and what does get injected
carries an explicit instruction to say she does not remember rather than invent.
The threshold is not a guess - scripts/iris_recall_gate.py measures the score
distribution of known-good hits against known-absent questions and prints the
separating value.

BOUNDED (RD-031): one HTTP call per recall-shaped utterance, hard timeout, at most
RECALL_K episodes, clause truncated to RECALL_MAX_CHARS. num_ctx is 6144 and a
camera frame alone is ~4570 tokens - see the arithmetic at RECALL_MAX_CHARS in
core/config.py. Vision turns never reach this code at all: ask_vision() and
ask_vision_game() build their own prompts and never call _build_messages().

DEFAULT OFF (RECALL_ENABLED=False). With the flag off this module makes no network
call, does no work, and returns "" - the prompt is byte-identical to pre-S224c.

Run `python core/recall.py --selftest` (no GPU, no network) to exercise it offline.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

# ── Intent: is this a question about something already said? ─────────────────
# Deterministic and LLM-free, the same class of cheap pre-LLM routing as
# services.llm.classify_response_length and is_story_continuation (S217).
#
# Deliberately a LITTLE liberal. A false positive costs one bounded HTTP call and
# then injects nothing, because the score gate rejects it; a false negative is a
# question she visibly fails to answer. The score threshold is the real filter,
# not this list.
_RECALL_PATTERNS = (
    # explicit memory appeals
    "do you remember", "you remember", "remember when", "remember that",
    "remember the", "do you recall", "recall when",
    # asking after the outcome of something already told
    "how did the story end", "how did that story end", "how did it end",
    "how did that end", "how did the story finish", "how did that finish",
    "what was the ending", "how does the story end",
    # games already played
    "who won", "did i win", "did you win", "did we win", "what was the score",
    "what's the score", "who was winning", "how many did i win",
    # things already said
    "what did we talk about", "what did we say", "what did i say",
    "what did you say", "what did i tell you", "what did i ask you",
    "what was that thing", "what was it called", "what did you call",
    # judgements already given
    "what did you think of", "what did you think about",
    "did you like my", "did you like the",
    # events already had
    "what happened to", "what happened with", "what happened at",
    "what happened when", "what happened after",
    # time-anchored recall
    "last time", "the other day", "yesterday we", "earlier you", "earlier we",
    "last week we", "the last one", "last night we",
    # S225: DECLARATIVE and IMPERATIVE framings. Every pattern above assumes
    # interrogative word order ("what DID we talk about"), and a person asking a
    # robot to remember mostly does not talk that way. Measured on the 44 unique
    # utterances of the 2026-07-22 evening failure: the old list matched 1 of 44.
    # The operator asked to be told what they had discussed three separate ways
    # and none of them routed, so retrieval never ran AND _NO_MEMORY never fired,
    # which left the model an open prompt to invent into. That is the mechanism
    # behind that night's hallucinations, not the retrieval itself -- the one
    # utterance that DID match ("what happened the last time Biscuit...", 08:37:48)
    # got the miss clause and she answered honestly.
    #
    # Scoped to the memory VERB so the bare imperative stays out: "tell me a
    # story about unicorns", "tell me the history of the Roman Empire" and "tell
    # me something funny" are all in that same 44 and must NOT route here -- a
    # false positive would drop a "you have nothing on this" clause into a story
    # request. Re-verified against all 44: 5 match, 0 false positives.
    "we talked about", "we talked", "we spoke about", "we discussed",
    "we did yesterday", "we said", "we went over", "we covered",
    "did talk about", "our conversation", "recalling the conversation",
    "remember the conversation", "anything about yesterday", "from yesterday",
)

# Structural rules, for the shapes a literal list cannot cover. "how did that
# unicorn story end" is the operator's own phrasing and matches none of the
# literals above, because the subject sits between the two halves of the pattern.
_RECALL_RULES = (
    (("how did",), ("end", "finish", "turn out", "go")),
    (("what did",), ("end up", "turn out")),
)

# Utterances that LOOK like recall but must never route here.
# "keep going" belongs to the S217 story-resume path, and injecting a stale
# memory mid-story would derail the story she is actually telling.
_RECALL_BLOCK = (
    "keep going", "keep telling", "carry on", "go on", "what happens next",
    "then what", "finish the story", "continue the story", "the rest",
    # present-tense opinion, not memory: "what do you think about X" is a
    # question about now. Only the PAST tense ("what did you think") is recall.
    "what do you think",
)

_MAX_QUERY_CHARS = 300      # a recall question is a sentence, not a transcript


def is_recall_question(text: str) -> bool:
    """True if the utterance asks about something from an earlier conversation."""
    t = (text or "").lower().strip()
    if not t:
        return False
    if any(p in t for p in _RECALL_BLOCK):
        return False
    if any(p in t for p in _RECALL_PATTERNS):
        return True
    return any(all(any(w in t for w in group) for group in rule)
               for rule in _RECALL_RULES)


# ── Retrieval ────────────────────────────────────────────────────────────────

_AUTH_HEADER = "X-IRIS-Auth"
_secret_cache = {}


def read_secret(path: str) -> str:
    """Read the shared corpus secret, cached per path. "" when there is none.

    A missing file is NOT an error: it yields "", no header is sent, and the
    server decides whether to serve an unauthenticated caller. That keeps the
    pre-RD-057 behavior available and means a secret that has not been deployed
    yet degrades recall to a miss rather than crashing a reply.
    """
    if path in _secret_cache:
        return _secret_cache[path]
    try:
        with open(path, "r", encoding="utf-8") as f:
            val = f.read().strip()
    except (OSError, ValueError):
        val = ""
    _secret_cache[path] = val
    return val


def search(query: str, url: str, k: int = 2, timeout: float = 2.0,
           opener=None, ns: str = "prod", secret: str = "") -> list:
    """POST the question to the GandalfAI retrieval service. Never raises.

    Recall is best-effort by design: a sleeping or busy GandalfAI must cost her
    the memory, never the reply. Every failure path returns [].

    That best-effort contract now also absorbs an auth or port mismatch, which is
    worth stating plainly because it is the risk RD-057 introduces: if the two
    sides disagree, this returns [] and she says she does not remember, exactly as
    if GandalfAI were asleep. It is silent by design at the reply, so the deploy
    check is `[RECALL] ... hits=N`, not "she still talks".
    """
    payload = {"q": (query or "")[:_MAX_QUERY_CHARS], "k": int(k)}
    if ns and ns != "prod":
        payload["ns"] = ns
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[_AUTH_HEADER] = secret
    req = urllib.request.Request(url, data=body, headers=headers)
    send = opener or urllib.request.urlopen
    try:
        with send(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("results") or []
    except Exception:
        return []


def ledger_lookup(query: str, url: str, k: int = 2, timeout: float = 2.0,
                  opener=None, ns: str = "prod", secret: str = "") -> dict:
    """POST to the RD-063 /recall endpoint. Never raises; {} on any failure.

    Returns the server's epistemic envelope: {result, facts, artifacts, results}.
    An empty dict means "could not ask", which the caller must treat as a MISS and
    not as NO_ANSWER -- they are different states. NO_ANSWER is a fact about the
    record; {} is a fact about the network, and only the first one licenses her to
    say she has nothing.
    """
    payload = {"q": (query or "")[:_MAX_QUERY_CHARS], "k": int(k)}
    if ns and ns != "prod":
        payload["ns"] = ns
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers[_AUTH_HEADER] = secret
    req = urllib.request.Request(url, data=body, headers=headers)
    send = opener or urllib.request.urlopen
    try:
        with send(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) and data.get("ok") else {}
    except Exception:
        return {}


# ── Clause composition ───────────────────────────────────────────────────────

_WRAP_OPEN = "(Context, not spoken: "

# The honesty instruction is not decoration. It is the one lever this module has
# against the confabulation logged at S224b, where she invented household
# observations in specific detail. Retrieved material is exactly the kind of thing
# a model will embroider, so the clause states the boundary every time.
#
# S224e REWROTE THIS, and the wording was chosen by measurement, not by taste.
# scripts/iris_recall_clause_ab.py drove four candidates plus the previous string
# through the real adult model with the real composer, n=10 per arm, same sitting
# (S215b BASELINE-DELTA). On the failing case - handed a REAL memory that does not
# answer the question - the previous string was clean in roughly 1 of 10 samples
# and INVENTED specific medical advice in 7 of 10 ("RICE: rest, ice, compression,
# elevation", "something about physical therapy", "the doctor's note was in your
# email"). This wording is clean in 8 of 10 and holds the grounded case at the
# same 9/10 the old one scored, so it costs nothing where recall actually works.
#
# WHAT ACTUALLY DID THE WORK, isolated by a fourth arm that failed: it is NOT the
# no-added-details phrasing. An arm that kept the old "Use this only if it
# actually answers what was just asked" opener and grafted on the new second half
# scored 2/10 - barely better than the old string. Replacing that conditional
# opener with a statement about the world ("that is what came back from your
# record") is what moved it, in both arms that did so. A conditional she has to
# remember to evaluate at the end of a long clause loses to a strong prior; a fact
# about her own situation does not.
#
# The second half then routes the wrong-memory case into the SAME state as the
# miss case, because _NO_MEMORY already measures 3/3 in her voice and there was no
# reason to invent a second way of saying the same thing. Framed by what to DO,
# with one thin backstop, per the operator's standing position that prohibition is
# whack-a-mole - the same reasoning written out at _NO_MEMORY below.
#
# LENGTH IS CHARGED AGAINST THE EPISODE BUDGET. clause() computes
# overhead = wrapper + prefix + _HONESTY, so a longer instruction leaves less room
# for the memory itself, and at RECALL_MAX_CHARS=700 that is exactly what produced
# the fabricated story ending S224d fixed. This is 249 chars against the old 180.
# The selftest's tight-cap case is the guard: keep it passing.
_HONESTY = ("That is what came back from your record. If it answers what was just "
            "asked, answer from it and add nothing. If it is about something else, "
            "then on this question you have nothing: say so plainly in your own "
            "voice and invite them to tell you about it.")

# THE MISS CLAUSE, and it is the most important text in this module (S224d).
#
# The S224d voice bench measured where she actually fabricates, and it is NOT the
# grounded path. With a memory in hand she stayed honest in every case, including
# two traps where the memory did not answer the question at all. With NOTHING in
# hand she invented in every case: a whole unicorn story she never told, "the
# notebook says you did" with no notebook data, and - asked about a hotel in Spain
# she had never been told about - a confident "Yes." plus a family trip, a pool
# and direct beach access, offered with a proposal to track prices.
#
# Silence is therefore not neutral. An empty clause hands the turn to the model's
# prior, which fills it. So a recall question that retrieves nothing gets told so
# explicitly, rather than getting nothing.
#
# Framed by what to DO, not by a list of prohibitions, per the operator's standing
# design position: prohibition is whack-a-mole, and the kids modelfile forbidding
# "(Note to self: ...)" by name while the model emits that exact string is the
# proof. One thin backstop at the end, no more.
_NO_MEMORY = ("(Context, not spoken: you have just checked your record of earlier "
              "conversations for this and found nothing. You genuinely do not have "
              "this one. Say so plainly and briefly in your own voice, then invite "
              "them to tell you about it so you have it next time. Answer only from "
              "what is actually in front of you in this conversation.)")

# ── RD-063 P2: the fact and artifact shelves ─────────────────────────────────
#
# _HONESTY and _NO_MEMORY ABOVE ARE NOT TOUCHED. Both were chosen by measurement
# against the live model at S224d/S224e (n=10 per arm, same sitting), not by
# taste, and the S224e arm that grafted new wording onto the old opener scored
# 2/10 against 8/10 for the rewrite. Re-tuning them here would silently spend that
# measurement. The strings below are NEW, for a case the old ones never covered.
#
# WHAT IS DIFFERENT ABOUT A FACT. An episode is a whole exchange and the clause
# has to hedge about whether it answers the question. A fact is a verbatim span
# somebody actually said, checked by exact substring at admission
# (scripts/iris_facts.py), so the clause can state a much narrower thing: this
# sentence was said, on this day, by someone in the house. That is a stronger
# claim about provenance and a WEAKER claim about meaning, and the wording has to
# carry both or she will elaborate the span into a story.
#
# Following the S224e finding exactly: open with a statement about the world, not
# a conditional she has to remember to evaluate at the end of a long clause.
_FACT_HONESTY = ("That is the whole of what your record holds on this, word for "
                 "word. Answer from it and add nothing to it. If it does not "
                 "answer what was just asked, then on this question you have "
                 "nothing: say so plainly and invite them to tell you about it.")

# An artifact is something SHE wrote - a story, a joke, an opinion she gave. The
# risk here is the opposite one: not that she invents, but that she recounts her
# own invention as though it were an event that happened. So the clause names the
# authorship explicitly.
_ARTIFACT_HONESTY = ("That is something you made up and said yourself, not "
                     "something that happened. Talk about it as your own telling. "
                     "If it does not answer what was just asked, say plainly that "
                     "you have nothing on that one.")

# She cannot tell voices apart (S201B), so the attribution is never a name.
_FACT_PREFIX = "this is from your record of earlier conversations, not something you are seeing now. "


def _when(ts: str, now: float = None) -> str:
    """'yesterday' / 'on Sunday' / 'back in March' - how a person dates a memory."""
    import datetime
    now = time.time() if now is None else now
    try:
        t = time.mktime(time.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return "earlier"
    # CALENDAR days, not elapsed hours. Last night at six is "yesterday" to a
    # person even though it is 0.7 of a day ago, and getting that wrong is exactly
    # the kind of small wrongness that makes a memory sound fake.
    days = (datetime.date.fromtimestamp(now) - datetime.date.fromtimestamp(t)).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "on " + time.strftime("%A", time.localtime(t))
    if days < 30:
        return "a few weeks ago"
    return "back in " + time.strftime("%B", time.localtime(t))


def _fit(text: str, limit: int) -> str:
    """Trim a single episode's excerpt at a SENTENCE boundary, never mid-word.

    An excerpt that stops mid-sentence reads as a memory she half-has, which is
    exactly the state that invites her to complete it herself.
    """
    text = (text or "").strip()
    if limit <= 0:
        return ""          # nothing fits: the caller drops the episode entirely
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for end in (". ", "! ", "? ", ".", "!", "?"):
        i = cut.rfind(end)
        if i > limit // 3:
            return cut[:i + len(end)].strip()
    i = cut.rfind(" ")
    return (cut[:i] if i > 0 else cut).strip()


def clause(results: list, min_score: float = 0.60, max_chars: int = 600,
           now: float = None) -> str:
    """Compose the injected clause, or "" when nothing clears the threshold."""
    kept = [r for r in (results or [])
            if isinstance(r, dict) and float(r.get("score") or 0.0) >= min_score]
    if not kept:
        return ""
    # NEVER TRUNCATE MID-EPISODE (S224d). The first build capped the whole clause
    # and sliced wherever it landed, which is how "how did that unicorn story end"
    # produced a FABRICATED ending: retrieval ranked the story's setup above its
    # ending, the cap then cut mid-word inside the setup, and she was handed the
    # beginning of a story and asked how it finished. The honesty instruction
    # cannot catch that, because half a story does not look unanswerable - it
    # looks like the right story. So episodes are now admitted whole or not at
    # all, and each gets an equal share of the budget.
    prefix = "this is from an earlier conversation, not something you are seeing now. "
    overhead = len(_WRAP_OPEN) + len(prefix) + len(_HONESTY) + 3
    budget = max_chars - overhead
    if budget < 80:
        return ""
    share = budget // max(1, len(kept))
    parts = []
    used = 0
    for r in kept:
        said_raw = (r.get("assistant") or "").strip()
        asked_raw = (r.get("user") or "").strip()
        if not said_raw:
            continue
        # The question that prompted it is context, not the memory: cap it hard so
        # a rambling user turn can never crowd out her actual answer.
        asked = _fit(asked_raw, min(120, share // 3))
        said = _fit(said_raw, share - len(asked) - 60)
        if not said:
            continue                   # nothing of it fits: drop it, never half it
        # "someone in the house" is the whole attribution. She cannot tell voices
        # apart, so anything more specific would be an assertion she cannot make.
        piece = '%s someone in the house asked "%s" and you answered: "%s"' % (
            _when(r.get("ts"), now), asked, said)
        if used + len(piece) > budget:
            break                      # drop the episode whole, never slice it
        parts.append(piece)
        used += len(piece) + 7         # " Also, "
    if not parts:
        return ""
    return _WRAP_OPEN + prefix + " Also, ".join(parts) + ". " + _HONESTY + ")"


def fact_clause(entries: list, min_score: float = 0.70, max_chars: int = 600,
                now: float = None, artifact: bool = False) -> str:
    """Compose the clause for a fact or artifact hit, or "" if nothing clears.

    Same vehicle and the same whole-or-nothing rule as clause() above: an entry is
    admitted complete or dropped, never sliced. The S224d fabricated story ending
    came from slicing a memory mid-way and handing her the half that looked like
    the right answer, and a half-quoted fact would fail the same way while looking
    even more authoritative, because a quote reads as exact.
    """
    honesty = _ARTIFACT_HONESTY if artifact else _FACT_HONESTY
    kept = [e for e in (entries or [])
            if isinstance(e, dict)
            and float(e.get("similarity") or 0.0) >= min_score
            and (e.get("evidence") or "").strip()]
    if not kept:
        return ""
    overhead = len(_WRAP_OPEN) + len(_FACT_PREFIX) + len(honesty) + 3
    budget = max_chars - overhead
    if budget < 60:
        return ""
    share = budget // max(1, len(kept))
    parts, used = [], 0
    for e in kept:
        span = (e.get("evidence") or "").strip()
        if len(span) > share:
            continue                   # drop it whole; never quote half a sentence
        if artifact:
            piece = '%s you told them: "%s"' % (_when(e.get("ts"), now), span)
        else:
            # "someone in the house" is the whole attribution: she has no speaker
            # recognition (S201B), so a name would be an assertion she cannot make.
            piece = '%s someone in the house said: "%s"' % (_when(e.get("ts"), now), span)
        if used + len(piece) > budget:
            break
        parts.append(piece)
        used += len(piece) + 7
    if not parts:
        return ""
    return _WRAP_OPEN + _FACT_PREFIX + " Also, ".join(parts) + ". " + honesty + ")"


# ── RD-063 P3: the deterministic no-answer bank ──────────────────────────────
#
# When the record genuinely has nothing, the honest reply does not need a model.
# _NO_MEMORY works by ASKING the model to admit a miss, which means a miss is
# still a generation and can still be embroidered -- the S224d bench measured her
# inventing on every ungrounded prompt. A code-decided line cannot invent, because
# there is no inference in the path at all.
#
# Kept in her register rather than as an error string: this is spoken aloud to a
# child as often as to an adult. Deliberately short, and none of them apologise
# twice or offer to go and check, because she cannot go and check.
_NO_ANSWER_BANK = (
    "No, nothing on that one. Tell me and I'll have it next time.",
    "Not a thing in my record about that. Fill me in?",
    "Drawing a blank on that one. What happened?",
    "Nope, I've got nothing there. Tell me about it.",
    "That one's not in my record. Go on then, tell me.",
)

# CONFLICTED is a DIFFERENT answer from a miss and must sound different. "I have
# two different answers" is honest; silently serving the higher-scoring one is
# exactly how a confident wrong memory is made.
_CONFLICT_BANK = (
    "I've got two different answers on that, so I'd rather you told me which.",
    "My record disagrees with itself there. Which is right?",
)


def no_answer_line(seed: int = 0, conflicted: bool = False) -> str:
    """Pick a deterministic refusal line. Pure: same seed, same line, no model."""
    bank = _CONFLICT_BANK if conflicted else _NO_ANSWER_BANK
    return bank[int(seed) % len(bank)]


# ── The one call assistant.py makes ──────────────────────────────────────────

# What the LAST prepare() decided, for the caller that has to record it.
#
# WHY THIS EXISTS (S224d, and it gates the whole feature): her fabrications become
# indexed memories. Verified by executing scripts/iris_episodes.py against the four
# fabrications the voice bench produced - all four pass the S224b sensory-claim
# filter and are stored as retrievable episodes. So she invents in adult mode, it
# drains to the corpus, and a later related question retrieves her own invention at
# a high score - at which point the honesty instruction works AGAINST us, because
# the material really does answer the question. Turning the flag off does not undo
# it; the corpus keeps what it wrote.
#
# `recall_question` is the one assistant.py acts on, and it quarantines the turn
# whether or not anything was retrieved. `grounded` is kept because it is the more
# interesting number to watch, not because it gates anything: the n=3 voice bench
# showed she also invents when handed a REAL memory that does not answer the
# question (offered a unicorn story, asked about a knee, she produced detailed
# medical advice in 2 of 3 samples). Her answer about the past is never itself
# evidence about the past - it either restates something already indexed, or it is
# invention - so none of it is indexed. Deterministic, no second model in the loop.
#
# RD-063 P3 adds two more keys. `epistemic` is what the ledger said (FOUND_FACT /
# FOUND_ARTIFACT / NO_ANSWER / AMBIGUOUS_OR_CONFLICTED, or "" when the ledger was
# not consulted), and `spoken_override` is a complete reply that assistant.py must
# speak VERBATIM instead of calling the model at all. The override exists because
# the honest miss is the one case where inference buys nothing and can only cost:
# there is no memory to phrase, so a code-decided line is both safer and faster.
last = {"recall_question": False, "grounded": False,
        "epistemic": "", "spoken_override": ""}


def _ledger_prepare(text, _cfg, t0):
    """The RD-063 fact-first path. Returns (clause, handled, episodes).

    handled=False means "fall through to the episode path" and is returned for
    every state that is not a confident ledger answer -- including the network
    failing, which must NOT be reported to her as "your record has nothing".

    `episodes` is the episode fallback /recall already returned, or None when the
    call failed. Handing it back is what keeps the flag ON costing the SAME ONE
    HTTP call the flag OFF costs: /recall ranks the shelves and the episodes from
    a single embedding of the query, so a second /search would re-embed the same
    sentence to get the same answer. Measured at ~57 ms per call on the live Pi,
    so the saving is small in absolute terms -- but the second call is also a
    second thing that can time out inside a 2 s budget, and that is the part worth
    not having.
    """
    ns = getattr(_cfg, "RECALL_NS", "prod")
    secret = read_secret(getattr(_cfg, "RECALL_SECRET_FILE",
                                 "/home/pi/corpus_secret.txt"))
    env = ledger_lookup(
        text,
        url=getattr(_cfg, "RECALL_LEDGER_URL", "http://192.168.0.20:8021/recall"),
        k=int(getattr(_cfg, "RECALL_K", 2)),
        timeout=float(getattr(_cfg, "RECALL_TIMEOUT_S", 2.0)),
        ns=ns, secret=secret)
    if not env:
        # Could not ask. NOT the same as "nothing found": fall back to the episode
        # path, which has its own miss handling and its own timeout already spent.
        # episodes=None means "I have nothing for you", so the caller does its own
        # /search rather than treating an empty list as a measured miss.
        return "", False, None

    result = env.get("result") or ""
    last["epistemic"] = result
    fmin = float(getattr(_cfg, "RECALL_FACT_MIN_SCORE",
                         getattr(_cfg, "RECALL_MIN_SCORE", 0.70)))
    maxc = int(getattr(_cfg, "RECALL_MAX_CHARS", 600))

    if result == "FOUND_FACT":
        out = fact_clause(env.get("facts"), min_score=fmin, max_chars=maxc)
        if out:
            return out, True, None
    elif result == "FOUND_ARTIFACT":
        out = fact_clause(env.get("artifacts"), min_score=fmin, max_chars=maxc,
                          artifact=True)
        if out:
            return out, True, None
    elif result == "AMBIGUOUS_OR_CONFLICTED":
        if getattr(_cfg, "RECALL_NO_ANSWER_BANK", False):
            last["spoken_override"] = no_answer_line(len(text or ""), conflicted=True)
            return "", True, None
        return _NO_MEMORY, True, None

    # NO_ANSWER, or a hit that did not clear the gate. The ledger is authoritative
    # about FACTS only, so a miss here still falls through to the episode path --
    # "how did that unicorn story end" is an episode question, not a fact question,
    # and demoting episodes entirely is a separate decision (see RD-063 Phase 4).
    return "", False, (env.get("results") or [])


def prepare(text: str, cfg=None) -> str:
    """Return the clause for this utterance, or "" (flag off, not recall, no hit).

    Never raises and never blocks longer than RECALL_TIMEOUT_S. Called once per
    routed utterance, from the same place the trajectory plan is refreshed.

    cfg is injectable so the selftest can PROVE the flag-off path makes no network
    call, rather than asserting it from reading the code.
    """
    if cfg is None:
        import core.config as cfg
    _cfg = cfg
    last["recall_question"] = False
    last["grounded"] = False
    last["epistemic"] = ""
    last["spoken_override"] = ""
    if not getattr(_cfg, "RECALL_ENABLED", False):
        return ""
    if not is_recall_question(text):
        return ""
    last["recall_question"] = True
    t0 = time.time()

    # RD-063: fact-first, behind a flag. With the flag OFF this branch is not
    # entered at all, so the composed prompt and the single /search call below are
    # byte-identical to pre-RD-063 -- proven in the selftest by a urlopen tripwire,
    # not asserted from reading this code.
    _led_eps = None
    if getattr(_cfg, "RECALL_LEDGER_ENABLED", False):
        led_out, handled, _led_eps = _ledger_prepare(text, _cfg, t0)
        if handled:
            last["grounded"] = bool(led_out)
            if getattr(_cfg, "RECALL_DEBUG", False):
                print("[RECALL] ledger result=%s injected=%s override=%s (%.0f ms)"
                      % (last["epistemic"], "yes" if led_out else "no",
                         "yes" if last["spoken_override"] else "no",
                         (time.time() - t0) * 1000.0), flush=True)
            return led_out

    # Reuse what /recall already ranked. With the flag OFF _led_eps is None and
    # this is the identical single search() call it has always been.
    if _led_eps is not None:
        hits = _led_eps
    else:
        hits = search(text,
                  url=getattr(_cfg, "RECALL_URL", "http://192.168.0.20:8021/search"),
                  k=int(getattr(_cfg, "RECALL_K", 2)),
                  timeout=float(getattr(_cfg, "RECALL_TIMEOUT_S", 2.0)),
                  ns=getattr(_cfg, "RECALL_NS", "prod"),
                  secret=read_secret(getattr(_cfg, "RECALL_SECRET_FILE",
                                             "/home/pi/corpus_secret.txt")))
    out = clause(hits,
                 min_score=float(getattr(_cfg, "RECALL_MIN_SCORE", 0.60)),
                 max_chars=int(getattr(_cfg, "RECALL_MAX_CHARS", 600)))
    last["grounded"] = bool(out)
    if not out and getattr(_cfg, "RECALL_MISS_CLAUSE", True):
        # Retrieval found nothing, so SAY so. An empty clause is not neutral here:
        # the bench showed the model fills the gap with invention. See _NO_MEMORY.
        #
        # RD-063 P3: with the bank on, do not ASK the model to admit the miss -
        # decide the whole reply here. _NO_MEMORY is an instruction and an
        # instruction can still be embroidered; a fixed line cannot, because no
        # inference runs. Default OFF, so this is opt-in and the bench-measured
        # _NO_MEMORY wording remains the fallback.
        if getattr(_cfg, "RECALL_NO_ANSWER_BANK", False):
            last["spoken_override"] = no_answer_line(len(text or ""))
            out = ""
        else:
            out = _NO_MEMORY
    if getattr(_cfg, "RECALL_DEBUG", False):
        top = hits[0] if hits else {}
        print("[RECALL] q=%r hits=%d top=%s score=%s injected=%s (%.0f ms)"
              % (text[:60], len(hits), top.get("id"), top.get("score"),
                 "yes" if out else "no", (time.time() - t0) * 1000.0), flush=True)
    elif out:
        print("[RECALL] memory folded in (%d chars, %.0f ms)"
              % (len(out), (time.time() - t0) * 1000.0), flush=True)
    return out


# ── selftest: offline, no GPU, no network ────────────────────────────────────

def _selftest():
    ran, fails = [], []

    def check(label, cond):
        ran.append(label)
        print("%-56s %s" % (label, "PASS" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    # intent - the operator's three questions must all route
    check("operator 1: how did the story end",
          is_recall_question("hey iris how did that unicorn story end"))
    check("operator 2: who won the last round",
          is_recall_question("who won the last round of rock paper scissors"))
    check("operator 3: what did you think of the drawing",
          is_recall_question("what did you think of the drawing Ava showed you"))
    check("do you remember", is_recall_question("do you remember the flying lizard one"))
    check("what did we talk about", is_recall_question("what did we talk about last night"))

    # intent - must NOT route
    check("ordinary question does not route",
          not is_recall_question("what's the capital of France"))
    check("story continuation does not route", not is_recall_question("keep going"))
    check("story continuation phrase does not route",
          not is_recall_question("what happens next"))
    check("present-tense opinion does not route",
          not is_recall_question("what do you think of my new haircut"))
    check("past-tense opinion DOES route",
          is_recall_question("what did you think of my haircut"))
    check("empty text safe", not is_recall_question(""))
    check("None safe", not is_recall_question(None))
    check("structural rule: subject between the halves",
          is_recall_question("how did that dragon story turn out"))
    check("what happened to X routes", is_recall_question("what happened to my tooth"))
    check("a blocked phrase beats a matching rule",
          not is_recall_question("how did it end? keep going"))

    # S225 declarative/imperative framings. These are the OPERATOR'S OWN WORDS,
    # transcribed, from the 2026-07-22 evening failure. The pre-S225 list matched
    # 1 of the 44 unique utterances that night: he asked to be told what they had
    # discussed three different ways and none of them routed, so neither a memory
    # nor the _NO_MEMORY guard reached the prompt and she invented instead.
    # Regression-locked here because the fix is pure data -- without these checks
    # the suite passes identically with the patterns deleted (verified: 51/51
    # before the S225 patterns and 51/51 after, i.e. they were gating nothing).
    for _u in ("tell me what we talked about today",
               "tell me what we spoke about yesterday",
               "tell me what we did yesterday",
               "we did talk about some stuff before",
               "seems you're hallucinating and not recalling the conversation"):
        check("S225 declarative routes: %r" % _u[:34], is_recall_question(_u))

    # The other half of the same fix, and the reason the patterns are scoped to a
    # memory verb rather than to "tell me". All three are also from that night's
    # 44. A false positive here is worse than a miss: it would drop "you have
    # checked your record and found nothing" into a request for a fresh story.
    for _u in ("tell me a story about unicorns, rainbows, sparkles, and something funny",
               "tell me the history of the roman empire",
               "tell me something funny",
               "what's the capital of australia",
               "let's play i spy"):
        check("S225 non-recall stays out: %r" % _u[:34], not is_recall_question(_u))

    # search never raises
    def _boom(req, timeout=None):
        raise OSError("gandalf is asleep")
    check("unreachable service returns no hits",
          search("anything", url="http://127.0.0.1:1/search", opener=_boom) == [])

    class _Resp:
        def __init__(self, b): self._b = b
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._b

    sent = {}

    def _ok(req, timeout=None):
        sent["body"] = json.loads(req.data.decode("utf-8"))
        sent["auth"] = req.get_header("X-iris-auth")
        return _Resp(json.dumps({"ok": True, "results": [
            {"id": "r1#1", "score": 0.81, "ts": "2026-07-20T18:05:00",
             "user": "one more", "assistant": "You win the last round."}]}).encode("utf-8"))

    got = search("who won", url="http://x/search", k=2, opener=_ok)
    check("search returns results", len(got) == 1 and got[0]["id"] == "r1#1")
    check("search sends q and k", sent["body"]["q"] == "who won" and sent["body"]["k"] == 2)

    # ── RD-057: auth header + namespace ──────────────────────────────────────
    # THE WIRE PAYLOAD FOR PRODUCTION IS UNCHANGED. "prod" is the default, so it
    # is omitted rather than sent, which means an old server and a new Pi still
    # understand each other on the request body and the ONLY breaking change in
    # the two-sided deploy is the port and the header. Fewer moving parts in the
    # window where the two sides disagree is the whole point.
    search("who won", url="http://x/search", k=2, opener=_ok)
    check("prod namespace is OMITTED, so the body is pre-RD-057 byte-identical",
          "ns" not in sent["body"])
    check("no secret means no auth header at all", sent["auth"] is None)
    search("who won", url="http://x/search", k=2, opener=_ok, ns="scratch1")
    check("a scratch namespace IS sent", sent["body"].get("ns") == "scratch1")
    search("who won", url="http://x/search", k=2, opener=_ok, secret="hunter2")
    check("a secret is sent as the auth header", sent["auth"] == "hunter2")
    check("the secret never leaks into the request body",
          "hunter2" not in json.dumps(sent["body"]))

    import tempfile as _tf
    with _tf.TemporaryDirectory() as _d:
        _p = os.path.join(_d, "sec.txt")
        with open(_p, "w", encoding="utf-8") as _f:
            _f.write("  s3cret \n")
        check("secret file is read and stripped", read_secret(_p) == "s3cret")
        check("a missing secret file yields no secret, not a crash",
              read_secret(os.path.join(_d, "absent.txt")) == "")
    check("query is length-capped",
          len(search("z" * 5000, url="http://x/search", opener=_ok) or []) == 1
          and len(sent["body"]["q"]) == _MAX_QUERY_CHARS)

    # clause
    now = time.mktime(time.strptime("2026-07-21T12:00:00", "%Y-%m-%dT%H:%M:%S"))
    hit = [{"id": "r1#1", "score": 0.81, "ts": "2026-07-20T18:05:00",
            "user": "one more", "assistant": "You win the last round."}]
    c = clause(hit, min_score=0.60, now=now)
    check("clause composed above threshold", c.startswith(_WRAP_OPEN) and c.endswith(")"))
    check("clause carries the honesty instruction", _HONESTY in c)
    check("clause never asserts who spoke",
          "someone in the house" in c and "you said" not in c.lower())
    check("clause dates the memory", "yesterday" in c)
    check("below threshold injects nothing",
          clause([dict(hit[0], score=0.42)], min_score=0.60, now=now) == "")
    check("no results injects nothing", clause([], min_score=0.60) == "")
    check("malformed results are survivable",
          clause([None, "junk", {}], min_score=0.0) == "")

    long_hit = [{"id": "r1#1", "score": 0.9, "ts": "2026-07-20T18:05:00",
                 "user": "u" * 300, "assistant": "a" * 600}]
    c2 = clause(long_hit, min_score=0.6, max_chars=600, now=now)
    check("clause respects max_chars", 0 < len(c2) <= 600)
    check("trimmed clause KEEPS the honesty instruction", _HONESTY in c2)
    check("impossible cap injects nothing rather than a naked memory",
          clause(long_hit, min_score=0.6, max_chars=120, now=now) == "")

    # THE REGRESSION TEST for the fabricated story ending (S224d). Two halves of a
    # story: the setup ranks first, the ending second. At the old 700-char cap the
    # clause was sliced mid-word inside the setup and the ending never arrived, so
    # she was handed the beginning of a story, asked how it ended, and invented one.
    part1 = "Once upon a time there was a unicorn called Persimmon. " + "S" * 500
    part2 = "And here is how it ended: she won first prize at the village fete. " + "E" * 300
    story = [{"id": "u#1", "score": 0.80, "ts": "2026-07-20T19:40:00",
              "user": "tell me a story about a unicorn", "assistant": part1},
             {"id": "u#2", "score": 0.78, "ts": "2026-07-20T19:40:00",
              "user": "keep going", "assistant": part2}]
    cs = clause(story, min_score=0.6, max_chars=1400, now=now)
    check("BOTH halves of a two-part story survive the cap",
          "Persimmon" in cs and "how it ended" in cs)
    check("no episode is ever sliced mid-word",
          "SS S" not in cs and not cs.replace(")", "").endswith("E"))
    # At a tight cap the pieces are trimmed at sentence boundaries rather than one
    # being dropped, which is BETTER than the rule originally written here: the
    # ending is what the question asked for, so it must survive the squeeze.
    tight = clause(story, min_score=0.6, max_chars=700, now=now)
    check("at a tight cap the story's ENDING still survives",
          "how it ended" in tight and _HONESTY in tight)
    check("tight clause still respects its budget", len(tight) <= 700)
    check("an episode that cannot fit at all is dropped, not emptied",
          '"' + '"' not in tight and 'answered: ""' not in tight)
    check("_fit trims at a sentence boundary",
          _fit("One. Two. Three that is far too long to keep", 22).endswith("."))

    # THE LENGTH CEILING, measured rather than guessed (S224e). _HONESTY is charged
    # against the episode budget, so lengthening it silently starves the memory
    # text - which is precisely how the fabricated story ending happened at S224d.
    # Swept empirically against the two-part story above at max_chars=700: 326
    # chars still keeps the ending, 327 loses it. 300 leaves working room without
    # sitting on the cliff. A future session that rewrites this string and trips
    # this check should re-run the sweep, not raise the number.
    check("honesty instruction leaves room for the memory itself",
          len(_HONESTY) <= 300)

    # the miss clause: silence is not neutral (S224d)
    check("miss clause names the absence", "found nothing" in _NO_MEMORY)
    check("miss clause invites them to tell her", "tell you" in _NO_MEMORY)
    check("miss clause is a Context clause, never a system message",
          _NO_MEMORY.startswith(_WRAP_OPEN) and _NO_MEMORY.endswith(")"))

    # THE FLAG-OFF PROOF. Not "the code returns early" - observed: with
    # RECALL_ENABLED False, prepare() must return "" AND urlopen must never be
    # reached, so the injected stamp is byte-identical to pre-S224c.
    class _Cfg:
        pass

    calls = []
    _real_urlopen = urllib.request.urlopen

    def _tripwire(req, timeout=None):
        calls.append(1)
        raise OSError("network touched with the flag off")

    urllib.request.urlopen = _tripwire
    try:
        off = _Cfg()
        off.RECALL_ENABLED = False
        check("flag OFF returns an empty clause",
              prepare("do you remember how that story ended", cfg=off) == "")
        check("flag OFF makes no network call", not calls)

        on_but_not_recall = _Cfg()
        on_but_not_recall.RECALL_ENABLED = True
        on_but_not_recall.RECALL_URL = "http://127.0.0.1:1/search"
        check("flag ON, non-recall utterance still makes no network call",
              prepare("what's the capital of France", cfg=on_but_not_recall) == ""
              and not calls)

        on_but_not_recall.RECALL_MISS_CLAUSE = False
        check("flag ON, recall utterance survives a dead service",
              prepare("do you remember the story", cfg=on_but_not_recall) == "")
        check("flag ON, recall utterance DID attempt the lookup", len(calls) == 1)

        # The quarantine signal that keeps a fabrication out of the corpus (S224d)
        check("ungrounded recall is reported as such",
              last["recall_question"] and not last["grounded"])
        prepare("what's the capital of France", cfg=on_but_not_recall)
        check("a non-recall turn is never flagged ungrounded",
              not last["recall_question"] and not last["grounded"])

        # with the miss clause on, a dead service still yields honest forgetting
        on_but_not_recall.RECALL_MISS_CLAUSE = True
        miss_out = prepare("do you remember the story", cfg=on_but_not_recall)
        check("miss clause is injected when retrieval finds nothing",
              miss_out == _NO_MEMORY)
        check("a miss is still flagged ungrounded for the quarantine",
              last["recall_question"] and not last["grounded"])
    finally:
        urllib.request.urlopen = _real_urlopen

    # ── RD-063 P2/P3: the ledger path ────────────────────────────────────────
    F = [{"id": "r1!1", "shelf": "fact", "similarity": 0.82,
          "ts": "2026-07-20T18:05:00", "source_role": "user",
          "evidence": "the bins go out on Tuesday night"}]

    fc = fact_clause(F, min_score=0.70, now=now)
    check("fact clause is composed above the gate",
          fc.startswith(_WRAP_OPEN) and fc.endswith(")"))
    check("fact clause quotes the span VERBATIM",
          '"the bins go out on Tuesday night"' in fc)
    check("fact clause carries the fact honesty instruction", _FACT_HONESTY in fc)
    check("fact clause never asserts WHO spoke",
          "someone in the house" in fc and "you said" not in fc.lower())
    check("fact clause dates the memory", "yesterday" in fc)
    check("below the fact gate injects nothing",
          fact_clause([dict(F[0], similarity=0.41)], min_score=0.70, now=now) == "")
    check("an entry with no evidence is dropped",
          fact_clause([dict(F[0], evidence="")], min_score=0.70, now=now) == "")
    check("malformed entries are survivable",
          fact_clause([None, "junk", {}], min_score=0.0, now=now) == "")
    # A quote is never half-quoted: a sliced fact reads MORE authoritative than a
    # sliced episode, because a quotation mark implies exactness (S224d class).
    long_f = [dict(F[0], evidence="z" * 900)]
    check("a span that cannot fit whole is dropped, never sliced",
          fact_clause(long_f, min_score=0.7, max_chars=600, now=now) == "")
    check("an impossible cap injects nothing",
          fact_clause(F, min_score=0.7, max_chars=100, now=now) == "")

    ac = fact_clause([dict(F[0], shelf="artifact",
                           evidence="Once upon a time there was a unicorn.")],
                     min_score=0.7, now=now, artifact=True)
    check("artifact clause names HER as the author",
          "you told them" in ac and _ARTIFACT_HONESTY in ac)
    check("artifact clause says it is not an event",
          "not something that happened" in ac)

    # the deterministic no-answer bank (P3)
    check("no-answer line is non-empty and short",
          0 < len(no_answer_line(0)) <= 90)
    check("no-answer is deterministic for a given seed",
          no_answer_line(3) == no_answer_line(3))
    check("the seed spreads across the bank",
          len({no_answer_line(i) for i in range(len(_NO_ANSWER_BANK))})
          == len(_NO_ANSWER_BANK))
    check("a conflict says something DIFFERENT from a miss",
          no_answer_line(0, conflicted=True) not in _NO_ANSWER_BANK)
    check("no bank line is a Context clause (these are SPOKEN)",
          all(not s.startswith(_WRAP_OPEN) for s in _NO_ANSWER_BANK + _CONFLICT_BANK))

    # THE FLAG-OFF PROOF, and it is the RD-063 P2 acceptance criterion. Not "the
    # branch is skipped" - observed: with RECALL_LEDGER_ENABLED False, exactly ONE
    # network call is made and it is the /search call, so the composed prompt is
    # byte-identical to pre-RD-063.
    urls = []

    def _count_urls(req, timeout=None):
        urls.append(req.full_url)
        return _Resp(json.dumps({"ok": True, "results": []}).encode("utf-8"))

    class _C2:
        pass

    off = _C2()
    off.RECALL_ENABLED = True
    off.RECALL_LEDGER_ENABLED = False
    off.RECALL_MISS_CLAUSE = True
    off.RECALL_URL = "http://x/search"
    off.RECALL_LEDGER_URL = "http://x/recall"
    off.RECALL_SECRET_FILE = os.devnull
    _real = urllib.request.urlopen
    urllib.request.urlopen = _count_urls
    try:
        out_off = prepare("do you remember the bins", cfg=off)
        check("ledger OFF still yields the S224d miss clause", out_off == _NO_MEMORY)
        check("ledger OFF makes EXACTLY ONE network call", len(urls) == 1)
        check("ledger OFF never touches /recall",
              urls == ["http://x/search"])
        check("ledger OFF reports no epistemic result", last["epistemic"] == "")
        check("ledger OFF sets no spoken override", last["spoken_override"] == "")

        # ON, and the ledger answers with a fact.
        def _recall_ok(req, timeout=None):
            urls.append(req.full_url)
            if req.full_url.endswith("/recall"):
                return _Resp(json.dumps({"ok": True, "result": "FOUND_FACT",
                                         "facts": F, "artifacts": [],
                                         "results": []}).encode("utf-8"))
            return _Resp(json.dumps({"ok": True, "results": []}).encode("utf-8"))

        on = _C2()
        on.RECALL_ENABLED = True
        on.RECALL_LEDGER_ENABLED = True
        on.RECALL_MISS_CLAUSE = True
        on.RECALL_FACT_MIN_SCORE = 0.70
        on.RECALL_URL = "http://x/search"
        on.RECALL_LEDGER_URL = "http://x/recall"
        on.RECALL_SECRET_FILE = os.devnull
        del urls[:]
        urllib.request.urlopen = _recall_ok
        got = prepare("do you remember the bins", cfg=on)
        check("ledger ON returns the fact clause", '"the bins go out on Tuesday night"' in got)
        check("ledger ON reports FOUND_FACT", last["epistemic"] == "FOUND_FACT")
        check("a confident fact costs ONE call, not two",
              urls == ["http://x/recall"])
        check("a fact hit is flagged grounded", last["grounded"])

        # NO_ANSWER must FALL THROUGH to episodes, not answer "you have nothing".
        # The ledger is authoritative about facts only; "how did that story end" is
        # an episode question and demoting episodes is a separate decision.
        def _recall_none(req, timeout=None):
            urls.append(req.full_url)
            if req.full_url.endswith("/recall"):
                return _Resp(json.dumps({"ok": True, "result": "NO_ANSWER",
                                         "facts": [], "artifacts": [],
                                         "results": []}).encode("utf-8"))
            return _Resp(json.dumps({"ok": True, "results": []}).encode("utf-8"))

        del urls[:]
        urllib.request.urlopen = _recall_none
        out_n = prepare("do you remember the bins", cfg=on)
        # NO_ANSWER falls through to the EPISODE PATH but NOT to a second HTTP
        # call: /recall already ranked the episodes from the same query embedding,
        # so re-asking /search would re-embed the same sentence for the same
        # answer. Flag ON therefore costs exactly the one call flag OFF costs.
        check("NO_ANSWER reuses the episodes /recall already returned",
              urls == ["http://x/recall"])
        check("and still ends at the measured miss clause", out_n == _NO_MEMORY)

        # But when /recall returns episodes that DO clear the gate, they are used.
        def _recall_eps(req, timeout=None):
            urls.append(req.full_url)
            return _Resp(json.dumps({"ok": True, "result": "NO_ANSWER",
                                     "facts": [], "artifacts": [],
                                     "results": [{"id": "e1", "score": 0.9,
                                                  "ts": "2026-07-20T18:05:00",
                                                  "user": "one more",
                                                  "assistant": "You win the last round."}]
                                     }).encode("utf-8"))

        del urls[:]
        urllib.request.urlopen = _recall_eps
        out_e = prepare("do you remember the bins", cfg=on)
        check("a /recall episode hit composes the normal episode clause",
              "You win the last round." in out_e and _HONESTY in out_e)
        check("and still costs only one call", urls == ["http://x/recall"])

        # A DEAD ledger is not the same as an empty one. If /recall cannot be
        # reached she must not be told her record is empty - that is a claim about
        # the record, and the network failing is not evidence about the record.
        def _recall_dead(req, timeout=None):
            urls.append(req.full_url)
            if req.full_url.endswith("/recall"):
                raise OSError("gandalf asleep")
            return _Resp(json.dumps({"ok": True, "results": []}).encode("utf-8"))

        del urls[:]
        urllib.request.urlopen = _recall_dead
        prepare("do you remember the bins", cfg=on)
        check("an unreachable ledger falls back rather than claiming emptiness",
              urls == ["http://x/recall", "http://x/search"])
        check("an unreachable ledger reports no epistemic result",
              last["epistemic"] == "")

        # CONFLICTED with the bank on: a code-decided spoken line, no inference.
        def _recall_conf(req, timeout=None):
            urls.append(req.full_url)
            return _Resp(json.dumps({"ok": True, "result": "AMBIGUOUS_OR_CONFLICTED",
                                     "facts": [], "artifacts": [],
                                     "results": []}).encode("utf-8"))

        on.RECALL_NO_ANSWER_BANK = True
        del urls[:]
        urllib.request.urlopen = _recall_conf
        out_c = prepare("do you remember the bins", cfg=on)
        check("a conflict injects no clause", out_c == "")
        check("a conflict sets a spoken override instead",
              last["spoken_override"] in _CONFLICT_BANK)
        check("a conflict does NOT fall through to episodes",
              urls == ["http://x/recall"])

        # And the miss path with the bank on: still one /search, but the reply is
        # decided in code rather than asked of the model.
        on.RECALL_LEDGER_ENABLED = False
        del urls[:]
        urllib.request.urlopen = _count_urls
        out_b = prepare("do you remember the bins", cfg=on)
        check("bank ON: a miss injects no clause", out_b == "")
        check("bank ON: a miss sets a spoken override",
              last["spoken_override"] in _NO_ANSWER_BANK)
        check("bank ON: the miss is still flagged ungrounded for the quarantine",
              last["recall_question"] and not last["grounded"])
    finally:
        urllib.request.urlopen = _real

    # relative dating
    check("today reads as earlier today", _when("2026-07-21T09:00:00", now) == "earlier today")
    check("last month reads as a month name",
          _when("2026-03-08T16:00:00", now).startswith("back in"))
    check("unparseable ts does not crash", _when("garbage", now) == "earlier")

    print("\n%d/%d PASS" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python core/recall.py --selftest")
