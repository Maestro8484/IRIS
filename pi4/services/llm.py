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
    text = re.sub(r'\*[^*]*\s+[^*]*\*', '', text)
    text = re.sub(r'_[^_]*\s+[^_]*_', '', text)
    # Ellipsis: normalize unicode "…" to ASCII dots first. Kokoro renders "..." as a
    # measured pause, so keep it (collapse any run of 2+ dots to exactly three). Piper
    # verbalizes "..." as "dot dot dot", so collapse to a single period when Kokoro is
    # disabled (Piper-primary configs). (S167)
    text = text.replace('…', '...')
    text = re.sub(r'\.{2,}', '...' if KOKORO_ENABLED else '.', text)
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


def _split_sentences(text: str) -> list:
    """
    Split text on sentence boundaries (.!?) followed by whitespace or end of string.
    Minimum 8 chars per chunk to avoid splitting abbreviations like Dr. or U.S.
    Returns list of non-empty stripped strings.
    """
    # Negative lookbehind keeps an ellipsis (".." / "...") intact within one chunk so
    # Kokoro renders its trailing pause, instead of fragmenting it into choppy separate
    # synths. Single-period sentence ends still split normally. (S167)
    parts = re.split(r'(?<=[.!?])(?<!\.\.)\s+', text.strip())
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


def stream_ollama(messages: list, model: str, num_predict: int):
    """
    Stream sentence-boundary chunks from Ollama /api/chat with stream=True.

    Yields (chunk_text, emotion) tuples:
      - First yield: emotion is the extracted [EMOTION:X] value (or 'NEUTRAL' if absent).
      - Subsequent yields: emotion is None.
      - chunk_text is a clean spoken sentence ready for TTS.

    Caller assembles full reply from chunks for history and followup detection.
    Raises RuntimeError on connection or HTTP failure so caller can handle gracefully.
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
                    # Flush all remaining buffer as final chunks
                    parts = _split_sentences(buffer)
                    for p in parts:
                        cleaned = clean_llm_reply(p)
                        if cleaned:
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
                        idx = max(buffer.rfind(', '), buffer.rfind('; '))
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
)


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

    # Heuristic: questions under 6 words with no complexity signals -> short
    if word_count <= 5 and t.endswith("?"):
        return _short

    # Heuristic: question word present and moderate length -> medium
    if any(t.startswith(qw) for qw in ("what", "who", "where", "when", "which")):
        if word_count <= 10:
            return _medium
        return _long

    # Default: medium
    return _medium
