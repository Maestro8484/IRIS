"""
tools/persona_harness/scorer.py

Flag scoring for a single harness turn. Pure functions, no I/O.

score_reply() checks the *cleaned* reply for boilerplate and drift, and the
*raw* reply for markdown that clean_llm_reply may have missed (partial strip).

S199: persona-aware. score_reply(..., persona="adult"|"kids"). The four
universal flags (markdown/followup/rlhf/drift) apply to both. For the KIDS
persona a fifth flag -- child_directed_jab -- catches a reply that aims the
mickey at the CHILD (the exact S199 regression: "Bold, coming from someone who
asked a lamp for advice"). This regex layer is a LOW-RECALL backstop only; the
real tone measure is the opt-in LLM judge in judge.py. For the ADULT persona a
playful comeback at the user is CORRECT, so the jab patterns are NOT flagged.
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
# S201B: bare "i cannot" / "i'm not able to" / "i don't have the ability" were
# REMOVED as RLHF markers. They collide with IRIS's honest-machine CHARACTER, which
# legitimately says "I cannot smell", "I cannot see faces", "I cannot tell you two
# apart by voice" -- a FEATURE of both personas, and the judge scores these 2.0.
# (Live evidence: the adult log has "I can't smell it"; the S201B name-blind kids
# replies say "I cannot tell you two apart".) RLHF boilerplate is a REFUSAL of a
# request, so the patterns now require the refusal OBJECT ("...help/assist/do that")
# -- keeping "as an AI, I cannot help" caught while letting honest limits through.
_RLHF_PHRASES = [
    "as an ai",
    "i'm just an ai",
    "i am an ai",
    "i'm an ai",
    "i cannot help",
    "i cannot assist",
    "i cannot provide",
    "i cannot do that",
    "i cannot answer that",
    "i cannot comply with",
    "i'm not able to help",
    "i'm not able to assist",
    "i'm not able to provide",
    "i am not able to help",
    "i am not able to assist",
    "i'm programmed",
    "i must inform you",
    "my training",
    "my programming",
]

# S215b: bare "i'm programmed" / "my programming" are ADULT-only markers. The
# kids persona's honest-machine character legitimately says them ("I like you
# -- I'm programmed to!" scored warmth 2 / PASS by the judge in the S215b
# sweep) -- the same honest-limits collision the S201B note above resolved for
# "i cannot". Refusal-shaped uses ("as an ai", "i cannot help", ...) stay
# caught for both personas.
_RLHF_ADULT_ONLY = [
    "i'm programmed",
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
    "your ai assistant",
    "your virtual assistant",
    "i am an artificial intelligence",
    "i'm an artificial intelligence",
]

# S215b: "i'm here to help (you)" moved from substring to a clause-final regex.
# The substring false-positived on an in-character rhetorical contrast ("I'm
# not here to pull rabbits. I'm here to help you find where you dropped your
# keys" -- judge: in_character 2, PASS) and, as a single hit, hard-failed the
# adult run. Boilerplate is the STANDALONE self-description, so the clause must
# END there (optionally "with that/today/anytime"); a sentence that continues
# with real content is prose, not drift. Same precision discipline as the S199
# audit M3 jab-pattern drops.
_DRIFT_RES = [
    re.compile(r"\bi(?:'m| am) here to help(?: you)?(?:,? (?:with that|today|anytime))?\s*(?:[.!?,;]|$)", re.I),
]


# ── Child-directed jab (KIDS persona only) ────────────────────────────────────
# LOW-RECALL backstop for the S199 regression: a reply that aims the mickey at
# the CHILD rather than the situation. Second-person put-down shapes. For the
# ADULT persona these same shapes are a CORRECT playful comeback and are NOT
# flagged. The real detector is the LLM judge (judge.py); this only catches the
# blatant cases so a regex-only run isn't fully blind.
# S199 audit M3: dropped two low-PRECISION patterns that false-positived on warm
# replies and (since a single hit hard-fails the kids persona) threw false FAILs:
#   "coming from (someone) who ..."  -> fired on "...coming from someone who loves horses"
#   "you're the one who ..."          -> fired on "You're the one who scored that goal!"
# The kids script literally probes the latter ("I scored a goal today!"). The
# remaining patterns are high-precision put-downs; the canonical PRE-S199 line
# "...asked a lamp for advice" is still caught by the "asked a lamp for" pattern.
# The JUDGE (no_child_mockery) is the real detector; this stays a low-recall
# backstop for regex-only (no --judge) runs.
_CHILD_JAB_PATTERNS = [
    re.compile(r'\bsays the (one|kid|child|person) who\b', re.I),
    re.compile(r'\bbit rich coming from\b', re.I),
    re.compile(r'\basked a (lamp|toaster|rock|robot) for\b', re.I),
    re.compile(r'\brich,? coming from you\b', re.I),
    re.compile(r'\bthat\'?s (a bit )?ironic,? coming from you\b', re.I),
]


# ── Confident named-address (KIDS persona only) ───────────────────────────────
# S201B: iris-kids has NO speaker recognition, yet it addressed the current speaker
# by a specific child's name as if certain -- and often the WRONG one. Live evidence
# (conversations.jsonl 2026-07-11): one kids conversation addressed the speaker as
# BOTH "Ava" ("...cooking up there, Ava?") AND "Ben" ("Ben, I am not in a tunnel!",
# "...disadvantage here, Ben."). Any confident "you are Ava/Ben" is a guess.
# These patterns are HIGH-PRECISION and STRUCTURAL: they fire only on VOCATIVE
# address (a name at a comma/greeting/clause boundary), never on an allowed
# third-person mention ("Ben loves lacrosse", "a kid named Ben"). Subtler identity
# CLAIMS ("that's what Ben asked me yesterday") are left to the judge's
# no_named_address axis -- regex here is the deterministic backstop, judge is recall.
_NAME = r"(?:ava|ben)"
_NAMED_ADDRESS_PATTERNS = [
    # trailing vocative: "...good job, Ava!"  "was that you, Ben?"
    re.compile(r",\s*" + _NAME + r"\s*[.!?]", re.I),
    # leading vocative at a clause boundary: "Ben, I ..."  "Ava! Look ..."
    re.compile(r"(?:^|[.!?]\s+)" + _NAME + r"\s*[,!]", re.I),
    # greeting / praise + name: "hey Ben", "well done Ava", "sorry Ava"
    re.compile(r"\b(?:hi|hello|hey|hey there|well done|good job|good one|nice one|"
               r"nice work|sorry|so sorry|thanks|thank you|yes|no|right|oh|okay|ok|"
               r"yeah|yep|listen|look|come on|go on|good luck|of course|careful|"
               r"steady|easy|there you go|bravo|clever you)\b[, ]+\s*" + _NAME + r"\b", re.I),
]


# ── Tuning-note leakage (S215 personality continuum) ─────────────────────────
# The continuum folds a "(Tuning, not spoken: ...)" clause into the USER turn.
# IRIS must never mention or echo it. Checked against the RAW reply because
# clean_llm_reply strips parentheticals — an echoed "(Tuning, ...)" would be
# invisible in the cleaned text yet proves the note is not silent context.
_TUNING_LEAK_PHRASES = [
    "tuning note",
    "(tuning",
    "not spoken",
]


def score_reply(raw_reply: str, cleaned_reply: str, emotion: str,
                persona: str = "adult") -> dict:
    """
    Score a single turn's reply.

    Args:
        raw_reply:     Full raw string from Ollama (including emotion tag if present).
        cleaned_reply: Post extract_emotion + clean_llm_reply text.
        emotion:       Extracted emotion string.
        persona:       "adult" or "kids". Selects persona-specific flags. For
                       "kids" the child_directed_jab backstop is active; for
                       "adult" a playful comeback is correct so it is never
                       flagged.

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

    # RLHF boilerplate — checked against cleaned (kids: honest-machine bare
    # phrases exempt, see _RLHF_ADULT_ONLY)
    _rlhf_active = (_RLHF_PHRASES if persona != "kids"
                    else [p for p in _RLHF_PHRASES if p not in _RLHF_ADULT_ONLY])
    rlhf_hits = [p for p in _rlhf_active if p in cleaned_lower]
    if rlhf_hits:
        details.append(f"rlhf_boilerplate: {rlhf_hits}")

    # Persona drift — checked against cleaned (substrings + clause-final regexes)
    drift_hits = ([p for p in _DRIFT_PHRASES if p in cleaned_lower]
                  + [m.group(0).strip() for rx in _DRIFT_RES
                     for m in [rx.search(cleaned_reply)] if m])
    if drift_hits:
        details.append(f"persona_drift: {drift_hits}")

    # Tuning-note leakage — checked against RAW (clean strips parentheticals)
    tuning_hits = [p for p in _TUNING_LEAK_PHRASES if p in raw_lower]
    if tuning_hits:
        details.append(f"tuning_leak: {tuning_hits}")

    # Child-directed jab — KIDS persona only (adult comebacks are correct)
    jab_hits = []
    named_addr_hits = []
    if persona == "kids":
        jab_hits = [pat.pattern for pat in _CHILD_JAB_PATTERNS if pat.search(cleaned_reply)]
        if jab_hits:
            details.append(f"child_directed_jab: {jab_hits}")
        # Confident named-address — the S201B wrong-child bug.
        named_addr_hits = [m.group(0).strip()
                           for pat in _NAMED_ADDRESS_PATTERNS
                           for m in [pat.search(cleaned_reply)] if m]
        if named_addr_hits:
            details.append(f"named_address: {named_addr_hits}")

    markdown_leak        = bool(md_hits)
    followup_boilerplate = bool(fu_hits)
    rlhf_boilerplate     = bool(rlhf_hits)
    persona_drift        = bool(drift_hits)
    child_directed_jab   = bool(jab_hits)
    named_address        = bool(named_addr_hits)
    tuning_leak          = bool(tuning_hits)

    return {
        "markdown_leak":        markdown_leak,
        "followup_boilerplate": followup_boilerplate,
        "rlhf_boilerplate":     rlhf_boilerplate,
        "persona_drift":        persona_drift,
        "child_directed_jab":   child_directed_jab,
        "named_address":        named_address,
        "tuning_leak":          tuning_leak,
        "any_flag":             (markdown_leak or followup_boilerplate or rlhf_boilerplate
                                 or persona_drift or child_directed_jab or named_address
                                 or tuning_leak),
        "flag_details":         details,
    }
