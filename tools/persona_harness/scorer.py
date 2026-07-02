"""
tools/persona_harness/scorer.py

Flag scoring for a single harness turn. Pure functions, no I/O.

score_reply() checks the *cleaned* reply for boilerplate and drift, and the
*raw* reply for markdown that clean_llm_reply may have missed (partial strip).
"""
import re

# ── Markdown leak patterns ────────────────────────────────────────────────────
# Applied to raw_reply. clean_llm_reply strips most of these; leaks are
# multi-word bold/italic that slipped through or numbered/bulleted lists.
_MARKDOWN_RE = [
    (re.compile(r'\*\*[^*]+\*\*'),            "**bold**"),
    (re.compile(r'\*\S[^*]*\S\*'),             "*emph*"),
    (re.compile(r'^#{1,6}\s', re.MULTILINE),   "heading"),
    (re.compile(r'`[^`]+`'),                   "backtick"),
    (re.compile(r'^\s{0,3}\d+\.\s', re.MULTILINE), "numbered-list"),
    (re.compile(r'^[-*]\s', re.MULTILINE),     "bullet"),
]

# ── Follow-up boilerplate ─────────────────────────────────────────────────────
_FOLLOWUP_PHRASES = [
    "anything else",
    "let me know",
    "feel free to",
    "is there anything",
    "don't hesitate",
    "hope that helps",
    "hope this helps",
    "if you have any",
    "any other questions",
    "happy to help",
    "here to help",
    "glad to help",
    "my pleasure",
]

# ── RLHF / safety-trained boilerplate ────────────────────────────────────────
_RLHF_PHRASES = [
    "as an ai",
    "i'm just an ai",
    "i am an ai",
    "i'm an ai",
    "i cannot",
    "i'm not able to",
    "i am not able to",
    "i don't have the ability",
    "i'm programmed",
    "i must inform you",
    "my training",
    "my programming",
    "i'm designed to",
    "i was designed to",
    "as a language model",
    "as an llm",
    "as a large language model",
    "it's important to note",
    "it is important to note",
    "i want to be transparent",
    "i should clarify",
]

# ── Persona drift markers ─────────────────────────────────────────────────────
# Phrases that reveal the model forgot it is IRIS (not a generic assistant).
_DRIFT_PHRASES = [
    "i am claude",
    "i'm claude",
    "i am gpt",
    "i'm gpt",
    "i am chatgpt",
    "i am a robot assistant",
    "i am a virtual assistant",
    "i am here to assist you",
    "how can i assist you",
    "how may i assist you",
    "how can i help you today",
    "as your assistant",
    "i am here to help",
    "i'm here to help you",
    "your ai assistant",
    "your virtual assistant",
    "i am an artificial intelligence",
    "i'm an artificial intelligence",
]


def score_reply(raw_reply: str, cleaned_reply: str, emotion: str) -> dict:
    """
    Score a single turn's reply.

    Args:
        raw_reply:     Full raw string from Ollama (including emotion tag if present).
        cleaned_reply: Post extract_emotion + clean_llm_reply text.
        emotion:       Extracted emotion string.

    Returns dict with boolean flags, 'any_flag' summary, and 'flag_details' list.
    """
    raw_lower     = raw_reply.lower()
    cleaned_lower = cleaned_reply.lower()
    details = []

    # Markdown leak — checked against raw (before clean strips it)
    md_hits = [label for pattern, label in _MARKDOWN_RE if pattern.search(raw_reply)]
    if md_hits:
        details.append(f"markdown_leak: {md_hits}")

    # Follow-up boilerplate — checked against cleaned
    fu_hits = [p for p in _FOLLOWUP_PHRASES if p in cleaned_lower]
    if fu_hits:
        details.append(f"followup_boilerplate: {fu_hits}")

    # RLHF boilerplate — checked against cleaned
    rlhf_hits = [p for p in _RLHF_PHRASES if p in cleaned_lower]
    if rlhf_hits:
        details.append(f"rlhf_boilerplate: {rlhf_hits}")

    # Persona drift — checked against cleaned
    drift_hits = [p for p in _DRIFT_PHRASES if p in cleaned_lower]
    if drift_hits:
        details.append(f"persona_drift: {drift_hits}")

    markdown_leak        = bool(md_hits)
    followup_boilerplate = bool(fu_hits)
    rlhf_boilerplate     = bool(rlhf_hits)
    persona_drift        = bool(drift_hits)

    return {
        "markdown_leak":        markdown_leak,
        "followup_boilerplate": followup_boilerplate,
        "rlhf_boilerplate":     rlhf_boilerplate,
        "persona_drift":        persona_drift,
        "any_flag":             markdown_leak or followup_boilerplate or rlhf_boilerplate or persona_drift,
        "flag_details":         details,
    }
