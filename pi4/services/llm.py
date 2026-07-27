#!/usr/bin/env python3
"""
services/llm.py - LLM output helpers and streaming interface

extract_emotion_from_reply and clean_llm_reply are pure functions with no
external dependencies -- safe to import anywhere.

stream_ollama() streams sentence-boundary chunks from Ollama /api/chat.
assistant.py's _speak_llm_turn() drives it for both the main turn and the
follow-up loop (the blocking ask_ollama() was retired in S126).
"""

import json
import re

import requests

from core.config import VALID_EMOTIONS, EMOTION_TAG_RE, EMOTION_TAG_ANY_RE, GANDALF, OLLAMA_PORT, KOKORO_ENABLED


def extract_emotion_from_reply(raw: str) -> tuple:
    """
    Parse [EMOTION:X] tag from the start of an LLM reply.
    Returns (emotion, cleaned_reply). Falls back to NEUTRAL if tag missing/invalid.
    """
    m = EMOTION_TAG_RE.match(raw)
    if m:
        emotion = m.group(1).upper()
        reply = raw[m.end():].strip()
        if emotion not in VALID_EMOTIONS:
            emotion = "NEUTRAL"
        return emotion, reply
    return "NEUTRAL", raw.strip()


def _strip_asides(text: str) -> str:
    """Remove parenthetical stage directions, counting depth instead of matching.

    S224e: the old r'\\s*\\([^)]*\\)' needed an OPEN and a CLOSE in the same string,
    and three real shapes do not have that. Verified by executing the deployed
    function, not by reading it:

      unclosed   "That was fun. (Note to self: add that to the"  -- the num_predict
                 guillotine lands inside the aside, so nothing ever closes it and
                 the whole fragment was spoken, brackets included.
      nested     "(Note (a side note) to self: do it.)" -- the inner pair matched
                 and was deleted, leaving "to self: do it.)" to be spoken. Same
                 corruption class as the S222 asterisk bug: a regex that cannot see
                 the pair it is inside eats the wrong span.
      split      a multi-sentence aside arrives here already cut in half by
                 _split_sentences, so each half carries one lone bracket. That one
                 is fixed at the splitter (see _split_sentences); the depth scan
                 covers what still reaches here.

    Depth counting handles all three. An unmatched OPEN drops to end of string,
    because everything after it is inside an aside she never closed. An unmatched
    CLOSE drops only the character -- deleting the text before it would risk eating
    a real sentence, and a lone ')' is inaudible noise rather than spoken words.
    """
    out = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == '(':
            if depth == 0:
                # keep what came before, minus the space the aside was sitting
                # behind, so "Hello. (aside) World" closes up to "Hello. World"
                out.append(text[start:i].rstrip())
                start = i
            depth += 1
        elif ch == ')':
            if depth > 0:
                depth -= 1
                if depth == 0:
                    start = i + 1          # drop the whole aside
            else:
                out.append(text[start:i])  # stray close: drop the character only
                start = i + 1
    if depth == 0:
        out.append(text[start:])
    # depth > 0: an aside was opened and never closed, so everything from its
    # opening bracket to the end of the reply is dropped with it.
    return ''.join(out)


def clean_llm_reply(text: str) -> str:
    """
    Strip markdown artifacts from LLM output.
    Leading emotion tag is handled by extract_emotion_from_reply/stream_ollama;
    this also strips any stray non-leading [EMOTION:X] tag so it's never spoken (S175).
    """
    text = EMOTION_TAG_ANY_RE.sub('', text)
    # Strip *multi-word action phrases* and _multi-word phrases_ entirely --
    # removes stage directions while preserving single-word emphasis
    # (e.g. *very* survives to the char-strip below as just "very")
    #
    # S222: the old pattern was r'\*[^*]*\s+[^*]*\*' and it CORRUPTED replies.
    # Because [^*]* cannot cross an asterisk, that pattern could not match a
    # pair of SINGLE-word emphases -- so the engine slid forward and matched the
    # GAP BETWEEN two pairs instead, deleting the real sentence text in between.
    # Measured on a live S221 reply: "how I *feel* when I *do* this, which *is* a
    # test" was spoken as "how I feeldois a test". It also ate one half of a
    # bracket pair, leaving an orphaned ")" for the paren strip below to miss.
    # Fix: match a PROPER pair, then decide by content. Multi-word -> drop (the
    # stage-direction case, behavior unchanged). Single word -> keep the word.
    _drop_if_multiword = lambda m: '' if re.search(r'\s', m.group(1)) else m.group(1)
    text = re.sub(r'\*([^*\n]+)\*', _drop_if_multiword, text)
    text = re.sub(r'_([^_\n]+)_', _drop_if_multiword, text)
    # Parenthetical asides / notes. This is a voice interface -- the persona is told
    # "nothing in brackets describing what you are doing" -- so "(Note to self: ...)"
    # or "(if Ava is speaking, ...)" is an artifact that must NEVER be spoken. Same
    # class as the *action* / _action_ stage directions stripped just above. S201B:
    # iris-kids, told it cannot tell which child is speaking, occasionally emitted a
    # tailoring note that both leaked a child's name and would have been read aloud.
    text = _strip_asides(text)
    # Ellipsis: normalize unicode "…" to ASCII dots first. Kokoro renders "..." as a
    # measured pause, so keep it (collapse any run of 2+ dots to exactly three). Piper
    # verbalizes "..." as "dot dot dot", so collapse to a single period when Kokoro is
    # disabled (Piper-primary configs). (S167)
    text = text.replace('…', '...')
    import core.config as _cfg          # live, not the frozen import (RD-047 follow-up)
    text = re.sub(r'\.{2,}', '...' if _cfg.KOKORO_ENABLED else '.', text)
    text = re.sub(r'[*_#`]', '', text)
    text = re.sub(r'^[-=]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Strip triple-dash separator and trailing meta-comment that follows it
    text = re.sub(r'\s*-{3,}.*', '', text, flags=re.DOTALL)
    text = re.sub(r'\n+', ' ', text)
    # Clean up orphaned numbered list artifacts: "1. : Have..." \u2192 "Have..."
    text = re.sub(r'\b\d+\.\s+:\s*', '', text)
    openers = [
        r"^okay[,!.]?\s+here[''\u2019s]*\s+(a|is|one)[^.]*[.!]?\s*",
        r"^here[''\u2019s]*\s+(a|is|one)[^.]*[.!]?\s*",
        r"^sure[,!.]?\s*",
        r"^of course[,!.]?\s*",
        r"^alright[,!.]?\s*",
        r"^absolutely[,!.]?\s*",
        r"^it sounds like you[^.!?]*[.!?]\s*",
        r"^as an (ai|artificial intelligence)[^.!]*[.!]\s*",
        r"^i'?m an (ai|artificial intelligence)[^.!]*[.!]\s*",
    ]
    for pat in openers:
        text = re.sub(pat, '', text, flags=re.IGNORECASE)
    # Strip trailing social filler sentences
    trailers = [
        r'\s*[Ff]eel free to (ask|reach out|contact)[^.!?]*[.!]',
        r'\s*[Ll]et me know if (you need|I can|there)[^.!?]*[.!]',
        r'\s*[Ii]f you have any (questions|requests|queries)[^.!?]*[.!]',
        r'\s*[Ii] hope (you enjoyed|this helped|this helps|this was)[^.!?]*[.!]',
        r'\s*[Ii]s there anything (else|more)[^.!?]*[?!]',
    ]
    for pat in trailers:
        text = re.sub(pat + r'$', '', text).strip()
    return text.strip()


# If a buffer grows past this without a sentence-end, yield at a comma/semicolon
# rather than holding it indefinitely. Rare for IRIS persona (short replies) but
# prevents worst-case stall on very long LLM sentences (S158 P1 review).
_MAX_SAFE_CHUNK = 200


def _last_pause_outside_aside(buf: str) -> int:
    """Index of the last ', ' or '; ' that is not inside an open parenthetical.

    -1 when there is none, which the caller already treats as "hold the buffer".
    """
    depth = 0
    best = -1
    for i, ch in enumerate(buf):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif depth == 0 and ch in ',;' and buf[i + 1:i + 2] == ' ':
            best = i
    return best


def _split_sentences(text: str) -> list:
    """
    Split text on sentence boundaries (.!?) followed by whitespace or end of string.
    Minimum 8 chars per chunk to avoid splitting abbreviations like Dr. or U.S.
    Returns list of non-empty stripped strings.
    """
    # Negative lookbehind keeps an ellipsis (".." / "...") intact within one chunk so
    # Kokoro renders its trailing pause, instead of fragmenting it into choppy separate
    # synths. Single-period sentence ends still split normally. (S167)
    #
    # S224e: a sentence end INSIDE an open parenthetical is not a boundary. The old
    # re.split cut "Right. (Note: This is not a joke request. I am being serious.)"
    # into two chunks with one lone bracket each, and clean_llm_reply -- which runs
    # per chunk on the streaming path and can only match a pair -- then spoke the
    # whole aside aloud. Measured on the deployed functions before the fix. Holding
    # the aside together lets the existing strip do its job.
    text = text.strip()
    depth_at = []
    _d = 0
    for _ch in text:
        if _ch == '(':
            _d += 1
        elif _ch == ')':
            _d = max(0, _d - 1)
        depth_at.append(_d)
    parts = []
    _pos = 0
    for _m in re.finditer(r'(?<=[.!?])(?<!\.\.)\s+', text):
        if _m.start() and depth_at[_m.start() - 1]:
            continue                       # inside an aside: not a boundary
        parts.append(text[_pos:_m.start()])
        _pos = _m.end()
    parts.append(text[_pos:])
    result = []
    carry = ""
    for part in parts:
        carry = (carry + " " + part).strip() if carry else part
        if len(carry) >= 8:
            result.append(carry)
            carry = ""
    if carry:
        result.append(carry)
    return [r for r in result if r.strip()]


# ── num_predict guillotine (S227) ────────────────────────────────────────────
#
# A reply that hits num_predict stops mid-word. Measured 2026-07-22: seven
# replies ended on "he's getting better at", "Now then", "you've got in", with
# no closing punctuation. S217 built a spoken bridge for this but wired it to
# the story tier, and it could not have fixed the cut anyway: the done frame
# below flushed the whole residual buffer BEFORE writing done_reason, so the
# fragment was already synthesized and spoken by the time any caller could know
# the reply had been guillotined.
#
# The rule is tier-agnostic on purpose. Truncation is a property of the stream,
# not of the tier, so MEDIUM, LONG, MAX, kids and vision follow-ups all get it.

_TERMINAL_PUNCT = ".!?…"


def _ends_complete(text: str) -> bool:
    """True if a chunk ends on real sentence-final punctuation. Closing quotes
    and brackets may follow it. An ellipsis counts (_split_sentences keeps one
    intact, and she uses it deliberately); a trailing dash or comma does not."""
    t = (text or "").rstrip().rstrip('"\'’”)]}')
    return bool(t) and t[-1] in _TERMINAL_PUNCT


def _drop_guillotined_tail(chunks: list, done_reason: str,
                           anything_yielded: bool) -> tuple:
    """Pure: decide what of the final flush is safe to speak.

    Returns (chunks_to_speak, dropped_or_None). Only a "length" stop drops
    anything, and only an unterminated LAST chunk. If dropping it would leave
    her having said nothing at all, it is kept -- a clipped word is bad, silence
    in reply to a question is worse.
    """
    if done_reason != "length" or not chunks:
        return chunks, None
    if _ends_complete(chunks[-1]):
        return chunks, None
    kept = chunks[:-1]
    dropped = chunks[-1]
    if not kept and not anything_yielded:
        return chunks, None
    return kept, dropped


def stream_ollama(messages: list, model: str, num_predict: int, meta: dict = None):
    """
    Stream sentence-boundary chunks from Ollama /api/chat with stream=True.

    Yields (chunk_text, emotion) tuples:
      - First yield: emotion is the extracted [EMOTION:X] value (or 'NEUTRAL' if absent).
      - Subsequent yields: emotion is None.
      - chunk_text is a clean spoken sentence ready for TTS.

    Caller assembles full reply from chunks for history and followup detection.
    Raises RuntimeError on connection or HTTP failure so caller can handle gracefully.

    meta (S217): optional caller-owned dict. On stream completion it receives
    done_reason ("stop" = natural end, "length" = num_predict guillotine) so the
    caller can tell a finished reply from a token-budget truncation.
    """
    url = f"http://{GANDALF}:{OLLAMA_PORT}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # No "think" key: iris is mistral-small3.2 (S119), which has no thinking mode.
        # (Was think:False for qwen3.5 in S114; Mistral silently ignores it -- removed as dead weight.)
        # keep_alive 8h (S134): Ollama's default is 5 min, so the 15GB model UNLOADS during
        # any conversational pause >5 min and the next reply pays a ~10-20 s cold reload --
        # the "reciprocal response delay" symptom (bench llm_ttfc spiked to 20.6 s cold vs
        # ~3 s warm). Gandalf is IRIS-dedicated (15GB iris + 2GB Kokoro = 17/24 GB), so pin
        # it resident through the awake day; it releases after 8 h idle (overnight),
        # re-warming on first morning use only.
        "keep_alive": "8h",
        "options": {"num_predict": num_predict},
    }

    emotion = "NEUTRAL"
    emotion_done = False
    first_yield = True
    buffer = ""
    _json_warn_fired = False

    try:
        with requests.post(url, json=payload, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    if not _json_warn_fired:
                        print("[LLM]  Malformed JSON line skipped (further skips suppressed)", flush=True)
                        _json_warn_fired = True
                    continue

                token = data.get("message", {}).get("content", "")
                buffer += token

                # Extract emotion tag from buffered content as soon as possible
                if not emotion_done:
                    stripped = buffer.lstrip()
                    m = EMOTION_TAG_RE.match(stripped)
                    if m:
                        emotion = m.group(1).upper()
                        if emotion not in VALID_EMOTIONS:
                            emotion = "NEUTRAL"
                        buffer = stripped[m.end():]
                        emotion_done = True
                    elif len(buffer) > 40:
                        # No tag found after 40 chars -- assume NEUTRAL and proceed
                        emotion_done = True

                done = data.get("done", False)

                if done:
                    # S227: read the stop reason BEFORE flushing. It used to be
                    # written after, so the mid-word fragment was already spoken
                    # by the time a caller could see "length".
                    _done_reason = data.get("done_reason", "")
                    if meta is not None:
                        meta["done_reason"] = _done_reason
                    # Flush remaining buffer as final chunks. Clean first, then
                    # judge completeness on what would actually be SPOKEN --
                    # cleaning drops unclosed asides and can change the ending.
                    _final = [c for c in (clean_llm_reply(p)
                                          for p in _split_sentences(buffer)) if c]
                    _final, _dropped = _drop_guillotined_tail(
                        _final, _done_reason, not first_yield)
                    if _dropped is not None:
                        print(f"[LLM]  num_predict guillotine at {num_predict} -- "
                              f"dropped {len(_dropped)} unterminated chars", flush=True)
                    for cleaned in _final:
                        out_emotion = emotion if first_yield else None
                        first_yield = False
                        yield cleaned, out_emotion
                    buffer = ""
                    _e_tok = data.get("eval_count", 0)
                    _p_tok = data.get("prompt_eval_count", 0)
                    _e_ms  = round(data.get("eval_duration", 0) / 1_000_000)
                    _p_ms  = round(data.get("prompt_eval_duration", 0) / 1_000_000)
                    print(f"[BENCH] stage=ollama_stats eval_tokens={_e_tok} prompt_tokens={_p_tok} eval_ms={_e_ms} prompt_ms={_p_ms}", flush=True)
                    break
                else:
                    # Yield complete sentences, hold last (may be incomplete)
                    parts = _split_sentences(buffer)
                    if len(parts) > 1:
                        for p in parts[:-1]:
                            cleaned = clean_llm_reply(p)
                            if cleaned:
                                out_emotion = emotion if first_yield else None
                                first_yield = False
                                yield cleaned, out_emotion
                        buffer = parts[-1]
                    elif len(buffer) > _MAX_SAFE_CHUNK:
                        # Comma/semicolon fallback: buffer is one very long partial
                        # sentence — yield at the last natural pause rather than hold.
                        # depth-aware, for the same reason the sentence splitter is
                        # (S224e): breaking at a comma INSIDE an aside hands the
                        # stripper two lone brackets and the aside gets spoken.
                        idx = _last_pause_outside_aside(buffer)
                        if idx > _MAX_SAFE_CHUNK // 3:
                            chunk_text = buffer[:idx + 1].strip()
                            buffer = buffer[idx + 2:].lstrip()
                            cleaned = clean_llm_reply(chunk_text)
                            if cleaned:
                                out_emotion = emotion if first_yield else None
                                first_yield = False
                                yield cleaned, out_emotion

    except Exception as e:
        raise RuntimeError(f"[LLM] stream_ollama failed: {e}") from e


# ── Response length classifier ─────────────────────────────────────────────────

# Patterns that signal a short answer is sufficient
_SHORT_PATTERNS = (
    "what time", "what's the time", "what day", "what date",
    "how old", "who made", "who created", "what is your name", "what's your name",
    "are you", "can you", "do you", "did you", "will you",
    "yes or no", "true or false",
    "hello", "hi iris", "hey iris", "good morning", "good night", "good evening",
    "thank you", "thanks", "okay", "ok", "got it", "nevermind", "never mind",
    "stop", "pause", "quit", "restart",
    "turn on", "turn off", "set volume", "volume up", "volume down",
    "what's the weather", "what is the weather",
    "remind me", "set a timer", "set timer",
    "random number", "pick a random", "tell me a random", "give me a random",
    "choose a random", "generate a random", "pick a number", "give me a number",
)

# Patterns that signal a long response is appropriate
_LONG_PATTERNS = (
    "explain", "explain to me", "explain how", "explain why", "explain what",
    "how does", "how do", "how would", "how should", "how can",
    "tell me about", "tell me everything", "tell me more",
    "what is the difference", "what's the difference", "compare",
    "walk me through", "walk me through it", "step by step", "step-by-step",
    "give me a list", "list of", "list the", "list all",
    "what are all the", "what are the different", "what are some",
    "write a", "write me a", "create a", "create me a",
    "make a list", "make me a",
    "story", "tell me a story", "tell a story",
    "recipe", "instructions for", "how to make", "how to build", "how to fix",
    "what are the steps", "what are the stages",
    "pros and cons", "advantages and disadvantages",
    "history of", "background on", "overview of",
    "describe", "describe the", "describe how",
    "what do you think about", "what do you think of",
    "give me your opinion", "give me advice",
    "debug", "troubleshoot", "diagnose",
    "brainstorm", "ideas for", "suggest some", "suggestions for",
    "in detail", "more detail", "more information", "more info",
    "elaborate", "expand on", "go deeper",
    "summary of", "summarize",
)

# Patterns that signal the MAX (story) tier -- the only tier that reaches ~1.5 min.
# S117: MAX is now reached ONLY by these EXPLICIT story / long-form-writing triggers.
# Everything else (explain/how/describe/list/...) tops out at LONG (~41 s).
# Do NOT add bare "story" here -- it substring-matches "history of ..." and would
# wrongly promote history questions to the story tier.
_MAX_PATTERNS = (
    # explicit story requests
    "tell me a story", "tell a story", "tell us a story", "read me a story",
    "make up a story", "a story about", "story about", "bedtime story",
    "short story", "full story", "long story",
    # explicit long-form writing requests
    "tell me everything", "everything about", "complete guide",
    "full explanation", "write a long", "write me a long", "write a detailed",
    "essay",
    # explicit story continuations (S217) -- unambiguous references to an
    # in-flight story get the full story budget even without resume state
    "keep telling the story", "continue the story", "keep the story going",
    "finish the story", "rest of the story", "more of the story",
    "next part of the story", "back to the story",
)


# S217: bare continuation phrases. Too ambiguous for _MAX_PATTERNS on their own
# ("keep going" mid-explanation is not a story), so assistant.py only consults
# is_story_continuation() while a truncated story is pending (_story_resume).
_STORY_CONTINUE_PATTERNS = (
    "keep going", "keep telling", "go on", "carry on", "continue",
    "what happens next", "then what", "what happened next",
    "next part", "don't stop", "and then", "tell me more", "more story",
    "finish it", "the rest",
)


def is_story_continuation(text: str) -> bool:
    """True if the utterance reads as 'continue the story you were telling'."""
    t = text.lower().strip()
    return any(p in t for p in _STORY_CONTINUE_PATTERNS)


def classify_response_length(text: str,
                              short: int = None,
                              medium: int = None,
                              long: int = None,
                              max_val: int = None) -> int:
    """
    Examine a user utterance and return an appropriate num_predict value.

    Falls back to config constants if overrides not provided.
    Priority: MAX > LONG > SHORT > MEDIUM (default).
    """
    # Import lazily to avoid circular imports
    from core.config import (
        NUM_PREDICT_SHORT  as _S,
        NUM_PREDICT_MEDIUM as _M,
        NUM_PREDICT_LONG   as _L,
        NUM_PREDICT_MAX    as _X,
    )
    _short  = short   if short   is not None else _S
    _medium = medium  if medium  is not None else _M
    _long   = long    if long    is not None else _L
    _max    = max_val if max_val is not None else _X

    t = text.lower().strip().rstrip(".!?,;:")
    words = t.split()
    word_count = len(words)

    # MAX tier
    if any(p in t for p in _MAX_PATTERNS):
        return _max

    # LONG tier -- never promotes to MAX (S117: MAX is story/long-form only).
    # A wordy "explain ..." question stays LONG (~41 s), it does not become a
    # ~1.5 min monologue just for being long.
    if any(p in t for p in _LONG_PATTERNS):
        return _long

    # SHORT tier -- only if clearly a simple query
    if any(t.startswith(p) or t == p for p in _SHORT_PATTERNS):
        return _short

    # Heuristic: questions under 6 words with no complexity signals -> short.
    # S227 REPAIR: this branch has never once fired. t is rstripped of ".!?,;:"
    # at the top of the function, so t.endswith("?") is unreachable and every
    # short question fell through to MEDIUM. Test the RAW utterance, which is
    # what it was always written to do. Measured 2026-07-22: "What's the capital
    # of Australia?" was handed a 224-token budget and used 28.
    if word_count <= 5 and text.strip().endswith("?"):
        return _short

    # S227: a bare reaction is not a request for content. "Fuck off.", "so",
    # "Oh" and "You're an idiot." all routed MEDIUM and got 224 tokens to fill.
    # Measured the same day: 41 of 47 turns routed MEDIUM, 2 routed SHORT.
    # Anything genuinely wanting length has already matched MAX or LONG above.
    if word_count <= 3:
        return _short

    # Heuristic: question word present and moderate length -> medium
    if any(t.startswith(qw) for qw in ("what", "who", "where", "when", "which")):
        if word_count <= 10:
            return _medium
        return _long

    # Default: medium
    return _medium


def apply_speed_bias(num_predict: int, speed: str) -> int:
    """
    S221 (trajectory Phase 2, DARK behind TRAJECTORY_ENABLED): nudge the tier
    verdict by the conversation's speed vector. Only the default MEDIUM verdict
    ever moves -- explicit verdicts (story MAX, LONG asks, SHORT lookups) always
    win over trajectory. Reads live config (not frozen imports) so WebUI tier
    edits keep applying (RD-047).
      speed "expansive" (Nth consecutive engaged turn): MEDIUM -> LONG
      speed "quip" (monosyllabic user reply):           MEDIUM -> SHORT
    """
    import core.config as _cfg
    if num_predict == _cfg.NUM_PREDICT_MEDIUM:
        if speed == "expansive":
            return _cfg.NUM_PREDICT_LONG
        if speed == "quip":
            return _cfg.NUM_PREDICT_SHORT
    return num_predict


# ── selftest: offline, no GPU, no network ────────────────────────────────────
#
# S224e. Gates the stage-direction repair. Run it on the Pi, where core.config
# imports cleanly. PYTHONPATH is required and is not decoration: this module does
# `from core.config import ...` at import time, and running the file directly puts
# services/ on sys.path instead of /home/pi, so core.* would not resolve.
#
#     cd /home/pi && PYTHONPATH=/home/pi python3 services/llm.py --selftest
#
# The cases are the ones that were MEASURED failing on the deployed functions,
# plus the ones that were already passing, because a fix that changes what
# already worked is a regression however good it looks on the failure.

def _selftest():
    ran, fails = [], []

    def check(label, cond):
        ran.append(label)
        print("%-62s %s" % (label, "PASS" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    def spoken(reply):
        """What actually reaches TTS on the streaming path: stream_ollama splits
        into sentences FIRST and cleans each chunk independently, so a per-chunk
        stripper is the thing under test, not clean_llm_reply on a whole reply."""
        return " ".join(c for c in (clean_llm_reply(p) for p in _split_sentences(reply)) if c)

    # ── the three shapes that were leaking (measured, not assumed) ────────────
    multi = "Right. (Note: This is not a joke request. I am being serious here.) Rest and ice."
    check("multi-sentence aside is not spoken",
          "(" not in spoken(multi) and "joke request" not in spoken(multi))
    check("multi-sentence aside keeps the real sentences",
          spoken(multi) == "Right. Rest and ice.")

    qmark = "Fine. (Should I write that down? Probably.) Anyway."
    check("aside containing a question mark is not spoken",
          spoken(qmark) == "Fine. Anyway.")

    unclosed = "That was fun. (Note to self: add that to the"
    check("unclosed aside (num_predict guillotine) is not spoken",
          spoken(unclosed) == "That was fun.")

    nested = "Good. (Note (a side note) to self: do it.) Done."
    check("nested aside is dropped whole, not half",
          spoken(nested) == "Good. Done.")

    # ── what already worked and must keep working ────────────────────────────
    kids = ("Oh! No idea -- but that's because Dad hasn't jotted it down yet. "
            "Tell me the best bit.\n\n(Note to self: add to notebook.)")
    check("the observed iris-kids note is still stripped",
          "Note to self" not in spoken(kids) and "Tell me the best bit." in spoken(kids))
    check("single-word emphasis still survives",
          clean_llm_reply("That is *very* good.") == "That is very good.")
    check("multi-word stage direction in asterisks still goes",
          clean_llm_reply("*leans forward* Right.") == "Right.")
    check("the S222 two-pair case is still correct",
          clean_llm_reply("how I *feel* when I *do* this") == "how I feel when I do this")
    check("plain text is untouched",
          spoken("Hello there. How are you?") == "Hello there. How are you?")
    check("empty in, empty out", clean_llm_reply("") == "" and _split_sentences("") == [])

    # ── the pieces, directly ─────────────────────────────────────────────────
    check("_strip_asides closes the gap left behind",
          _strip_asides("Hello. (aside) World") == "Hello. World")
    check("_strip_asides drops a stray close bracket only",
          _strip_asides("Hello) World") == "Hello World")
    check("_strip_asides leaves bracket-free text alone",
          _strip_asides("Hello World") == "Hello World")
    # The aside survives as ONE chunk, so clean_llm_reply sees a matched pair and
    # can strip it. Before the fix this split into "Right then." + "(Note: one."
    # + "Two.) All good here." and the middle two were spoken with their brackets.
    check("_split_sentences does not break inside an aside",
          _split_sentences("Right then. (Note: one. Two.) All good here.")
          == ["Right then.", "(Note: one. Two.) All good here."])
    check("_split_sentences still breaks normally outside one",
          _split_sentences("One. Two. Three.") == ["One. Two.", "Three."])
    check("_split_sentences keeps an ellipsis intact",
          _split_sentences("Wait... I remember that one.") == ["Wait... I remember that one."])
    check("comma fallback ignores a comma inside an aside",
          _last_pause_outside_aside("aaa (bb, cc) dd, ee") == 15)
    check("comma fallback returns -1 when every pause is inside one",
          _last_pause_outside_aside("aaa (bb, cc) dd") == -1)

    # ── S227: the num_predict guillotine ─────────────────────────────────────
    #
    # The seven tails below are the VERBATIM endings of the seven replies that
    # hit eval_tokens=224 on 2026-07-22, read out of the journal. Each one was
    # spoken as-is. None of them may ever be spoken again.

    check("terminal punctuation is complete", _ends_complete("Right then."))
    check("question mark is complete", _ends_complete("What's that?"))
    check("closing quote after a stop is complete", _ends_complete('He said "no."'))
    check("ellipsis is complete", _ends_complete("Well... maybe."))
    check("a bare word is not complete", _ends_complete("Now then") is False)
    check("a trailing comma is not complete", _ends_complete("first, then") is False)
    check("a trailing dash is not complete", _ends_complete("he was going to-") is False)
    check("empty is not complete", _ends_complete("") is False and _ends_complete("   ") is False)

    _MEASURED_TAILS = (
        "You can show an AI every picture of a cat you've got in",
        "It's like they took the best bits of Roman administration and",
        "It means you're letting the fact",
        "It doesn't work",
        "What's really got you stuck tonight",
        "Now then",
        "Then we dealt with Ben's maths homework because he's getting better at",
    )
    for _tail in _MEASURED_TAILS:
        _kept, _drop = _drop_guillotined_tail(["A finished sentence.", _tail],
                                              "length", True)
        check("measured tail dropped: %r" % _tail[-28:],
              _kept == ["A finished sentence."] and _drop == _tail)

    check("a natural stop drops nothing, even unterminated",
          _drop_guillotined_tail(["Fine.", "Now then"], "stop", True)
          == (["Fine.", "Now then"], None))
    check("a complete last sentence survives a length stop",
          _drop_guillotined_tail(["Fine.", "All done."], "length", True)
          == (["Fine.", "All done."], None))
    check("an empty flush is a no-op",
          _drop_guillotined_tail([], "length", True) == ([], None))
    # Silence is worse than a clipped word: if the fragment is the ONLY thing
    # she said this turn, it is kept and spoken.
    check("the only chunk in the whole reply is kept",
          _drop_guillotined_tail(["Now then"], "length", False)
          == (["Now then"], None))
    check("but it is dropped once something was already spoken",
          _drop_guillotined_tail(["Now then"], "length", True) == ([], "Now then"))

    # ── S227: tier routing ───────────────────────────────────────────────────
    from core.config import (NUM_PREDICT_SHORT as _S, NUM_PREDICT_MEDIUM as _M,
                             NUM_PREDICT_LONG as _L, NUM_PREDICT_MAX as _X)

    # The four the operator named, plus the two the journal added.
    for _u in ("Oh", "What?", "You're an idiot.", "Fuck off.", "so", "What's wrong?"):
        check("bare reaction -> SHORT: %r" % _u,
              classify_response_length(_u) == _S)
    check("short question -> SHORT (the repaired branch)",
          classify_response_length("What's the capital of Australia?") == _S)

    # What must NOT move. Six of the seven capped replies came from utterances
    # in this shape; they route MEDIUM correctly and the guillotine fix, not
    # routing, is what protects them.
    for _u in ("nothing I was just thinking about how robots work",
               "back up so you're talking about the Roman Empire",
               "I think you're hallucinating. Is that accurate?",
               "What do you see right now?"):
        check("stays MEDIUM: %r" % _u[:34], classify_response_length(_u) == _M)
    check("explicit story still MAX",
          classify_response_length("tell me a story about a dragon") == _X)
    check("explain still LONG", classify_response_length("explain gravity") == _L)
    check("four-word story ask still beats the reaction rule",
          classify_response_length("tell me a story") == _X)

    print("\n%d/%d PASS" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv:
        _sys.exit(_selftest())
    print("usage: python3 services/llm.py --selftest")
