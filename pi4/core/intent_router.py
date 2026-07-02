"""
core/intent_router.py - Pre-LLM intent classification for IRIS
5-layer: REFLEX -> COMMAND -> UTILITY -> AMBIGUOUS -> LLM
Fail-open: classify() exception always falls through to LLM.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# ── Route / action / confidence constants ─────────────────────────────────────
ROUTE_REFLEX    = "REFLEX"
ROUTE_COMMAND   = "COMMAND"
ROUTE_UTILITY   = "UTILITY"
ROUTE_AMBIGUOUS = "AMBIGUOUS"
ROUTE_LLM       = "LLM"

CONF_HIGH   = "HIGH"
CONF_MEDIUM = "MEDIUM"
CONF_LOW    = "LOW"


@dataclass
class IntentResult:
    route:      str
    action:     str
    confidence: str
    response:   Optional[str] = None
    payload:    Optional[dict] = None


# ── Rotating intent log ───────────────────────────────────────────────────────
_INTENT_LOG_PATH = "/home/pi/logs/iris_intent.log"


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("iris.intent")
    if logger.handlers:
        return logger
    try:
        os.makedirs(os.path.dirname(_INTENT_LOG_PATH), exist_ok=True)
        h = logging.handlers.TimedRotatingFileHandler(
            _INTENT_LOG_PATH, when="midnight", backupCount=365, encoding="utf-8"
        )
        h.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(h)
    except Exception:
        logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


_intent_logger = _build_logger()


def _log(raw: str, norm: str, result: IntentResult, llm: bool, extra: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = (
        f'{ts} | raw="{raw}" | norm="{norm}" | intent={result.action}'
        f" | route={result.route} | confidence={result.confidence} | llm={str(llm).lower()}"
    )
    if extra:
        line += f" | result={extra}"
    try:
        _intent_logger.info(line)
    except Exception:
        pass


# ── Text normalizer ────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    return text.lower().strip().strip(".!?,;:").strip()


# ── Layer 0: REFLEX ───────────────────────────────────────────────────────────

_SLEEP_EXACT = {
    "go to sleep", "go to sleep iris", "goodnight", "good night", "good night iris",
    "sleep now", "sleep mode", "time to sleep", "go to bed", "bedtime",
    "sleep iris", "iris sleep", "night night", "nighty night",
}
_SLEEP_STARTS = ("go to sleep", "goodnight", "good night", "sleep now", "bedtime")

_STOP_EXACT = {
    "stop", "cancel", "quiet", "stop talking", "shut up", "be quiet",
    "stop it", "ok stop", "please stop", "jarvis stop", "pause", "pause it",
}
_STOP_STARTS = ("stop", "shut up", "stop talking", "cancel", "quiet", "be quiet", "pause")

_WAKE_EXACT = {"wake up", "wake up iris", "iris wake up"}
_WAKE_STARTS = ("wake up",)

# Medium-confidence stop phrases -- routed to AMBIGUOUS
_AMBIGUOUS_STOP_EXACT = {"that's enough", "thats enough", "enough", "never mind", "nevermind"}

# Vague fatigue/sleepy phrases -- context-dependent
_AMBIGUOUS_SLEEPY = {"sleepy", "i'm sleepy", "im sleepy", "i'm tired", "im tired", "getting sleepy"}


# ── Layer 1: COMMAND ──────────────────────────────────────────────────────────

_VOL_UP_RE  = re.compile(r"volume up|louder|turn it up|increase volume|turn up|raise volume|higher volume|more volume")
_VOL_DN_RE  = re.compile(r"volume down|quieter|turn it down|decrease volume|lower volume|turn down|reduce volume|less volume|softer|too loud")
_VOL_MAX_RE = re.compile(r"all the way up|max volume|volume max|full volume|maximum volume|as loud")
_VOL_MIN_RE = re.compile(r"all the way down|volume low|minimum volume|volume minimum|as quiet")
_VOL_QRY_RE = re.compile(r"what'?s the volume|current volume|volume level|how loud|what volume")
_VOL_PCT_RE = re.compile(r"(\d+)\s*(?:percent|%)")

_KIDS_ON_RE = re.compile(
    r"kids mode on|enable kids mode|turn on kids mode|switch to kids mode|"
    r"kids mode please|activate kids mode|children'?s mode on|kid mode on"
)
_KIDS_OFF_RE = re.compile(
    r"kids? mode off|kids? mode stop|turn off kids? mode|disable kids? mode|"
    r"deactivate kids? mode|kid mode off|"
    r"(?:exit|leave|stop|quit|end|cancel) (?:the )?kids? mode|no more kids? mode|"
    r"switch (?:to |me to |back to )?(?:adult|normal|grown ?up|big ?kid)s? mode|"
    r"go (?:to |back to )(?:adult|normal|grown ?up)s? mode|"
    r"\b(?:adults?|normal|grown ?up|big ?kids?) mode\b|"
    r"back to normal|be normal|talk normal(?:ly)?"
)

# Eye control: pulled from core.config at import time to stay in sync
try:
    from core.config import EYES_SLEEP_TRIGGERS as _EYES_OFF_TRIGGERS
    from core.config import EYES_WAKE_TRIGGERS  as _EYES_ON_TRIGGERS
    from core.config import VISION_TRIGGERS      as _VISION_TRIGGERS
except ImportError:
    _EYES_OFF_TRIGGERS = set()
    _EYES_ON_TRIGGERS  = set()
    _VISION_TRIGGERS   = set()


# ── Layer 2: UTILITY ──────────────────────────────────────────────────────────

_TIME_RE = re.compile(
    r"what time is it|what'?s the time|tell me the time|time is it|time please"
    r"|current time\b(?!\s+travel)"
    r"|what time\b(?!\s+(?:did|was|were|happened)\b)"
    r"|what hour\b(?!\s+(?:did|was|were|happened)\b)"
)
_DATE_RE = re.compile(
    r"what'?s the date|today'?s date|day is it|date is it"
    r"|what\s+(?:year|month|day|date)\b(?!\s+(?:did|was|were|happened|built|born|died|founded|invented|started|ended)\b)"
)

# Random number request patterns
_RANDOM_RE = re.compile(
    r"\b(pick|choose|give|tell|generate|select|get)\b.{0,30}\brandom\s+(number|integer|digit|num)\b"
    r"|\brandom\s+(number|integer|digit)\b"
    r"|\bpick\s+a\s+number\b|\bgive\s+me\s+a\s+number\b"
)
_RANDOM_RANGE_RE = re.compile(r"\b(?:between|from)\s+([\d,]+)\s+(?:and|to)\s+([\d,]+)\b")


def _random_number_reply(norm: str) -> str:
    m = _RANDOM_RANGE_RE.search(norm)
    if m:
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        if lo > hi:
            lo, hi = hi, lo
        return f"{random.randint(lo, hi)}."
    if re.search(r"\bdigit\b", norm):
        return f"{random.randint(0, 9)}."
    return f"{random.randint(1, 100)}."


_MATH_PREFIXES = ("what is ", "what's ", "calculate ", "compute ", "solve ", "evaluate ")
_MATH_WORD_OPS = [
    ("multiplied by", "*"), ("divided by", "/"), ("times", "*"),
    ("plus", "+"), ("minus", "-"), ("over", "/"),
]
_MATH_SAFE_RE = re.compile(r"^[\d\s\+\-\*\/\(\)\.]+$")


def _parse_math(norm: str) -> Optional[str]:
    expr = None
    for prefix in _MATH_PREFIXES:
        if norm.startswith(prefix):
            expr = norm[len(prefix):].strip()
            break
    if expr is None or not any(c.isdigit() for c in expr):
        return None
    for word, op in _MATH_WORD_OPS:
        expr = re.sub(r"\b" + re.escape(word) + r"\b", f" {op} ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s+", " ", expr.replace(",", "").replace("×", "*").replace("÷", "/")).strip()
    if not _MATH_SAFE_RE.match(expr):
        return None
    try:
        result = eval(expr, {"__builtins__": {}}, {})  # noqa: S307
        if isinstance(result, float) and result.is_integer():
            return str(int(result))
        return str(round(result, 6))
    except Exception:
        return None


def _time_date_reply(norm: str) -> Optional[str]:
    is_time = bool(_TIME_RE.search(norm))
    is_date = bool(_DATE_RE.search(norm))
    if not (is_time or is_date):
        return None
    now = time.localtime()
    hour = now.tm_hour; minute = now.tm_min
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    if minute == 0:
        time_str = f"{hour12} {period}"
    elif minute < 10:
        time_str = f"{hour12} oh {minute} {period}"
    else:
        time_str = f"{hour12} {minute} {period}"
    day_name   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][now.tm_wday]
    month_name = ["January","February","March","April","May","June","July",
                  "August","September","October","November","December"][now.tm_mon - 1]
    if is_time and is_date:
        return f"It is {time_str} on {day_name}, {month_name} {now.tm_mday}."
    elif is_time:
        return f"It is {time_str}."
    else:
        return f"Today is {day_name}, {month_name} {now.tm_mday}, {now.tm_year}."


# ── Router ────────────────────────────────────────────────────────────────────

class IntentRouter:
    """Pre-LLM intent classifier. classify() never raises -- fails open to LLM."""

    def classify(self, raw: str, state=None) -> IntentResult:
        try:
            return self._classify(raw, state)
        except Exception as e:
            print(f"[ROUTE] classify() exception ({e}) -- falling through to LLM", flush=True)
            result = IntentResult(ROUTE_LLM, "LLM", CONF_LOW)
            _log(raw, _normalize(raw), result, llm=True)
            return result

    def _classify(self, raw: str, state) -> IntentResult:
        norm = _normalize(raw)
        result = (
            self._layer0_reflex(norm)
            or self._layer1_command(norm)
            or self._layer2_utility(norm)
            or self._layer3_ambiguous(norm, state)
            or IntentResult(ROUTE_LLM, "LLM", CONF_HIGH)
        )
        llm = result.route == ROUTE_LLM
        extra = str(result.payload["result"]) if result.payload and "result" in result.payload else ""
        _log(raw, norm, result, llm=llm, extra=extra)
        return result

    # ── Layer 0 ───────────────────────────────────────────────────────────────
    @staticmethod
    def _starts_phrase(norm: str, prefixes) -> bool:
        # Exact match or phrase followed by a space — avoids false matches on
        # "stopwatch", "quietly", "cancelled" (mirrors assistant.py STOP gate).
        return any(norm == p or norm.startswith(p + " ") for p in prefixes)

    def _layer0_reflex(self, norm: str) -> Optional[IntentResult]:
        if norm in _SLEEP_EXACT or self._starts_phrase(norm, _SLEEP_STARTS):
            return IntentResult(ROUTE_REFLEX, "SLEEP", CONF_HIGH, response="Goodnight.")
        if norm in _STOP_EXACT or self._starts_phrase(norm, _STOP_STARTS):
            return IntentResult(ROUTE_REFLEX, "STOP", CONF_HIGH)
        if norm in _WAKE_EXACT or self._starts_phrase(norm, _WAKE_STARTS):
            return IntentResult(ROUTE_REFLEX, "WAKE", CONF_HIGH)
        return None

    # ── Layer 1 ───────────────────────────────────────────────────────────────
    def _layer1_command(self, norm: str) -> Optional[IntentResult]:
        if norm in _EYES_OFF_TRIGGERS:
            return IntentResult(ROUTE_COMMAND, "EYES_SLEEP", CONF_HIGH)
        if norm in _EYES_ON_TRIGGERS:
            return IntentResult(ROUTE_COMMAND, "EYES_WAKE", CONF_HIGH)
        if _KIDS_ON_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "KIDS_ON", CONF_HIGH)
        if _KIDS_OFF_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "KIDS_OFF", CONF_HIGH)
        if _VOL_MAX_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "VOLUME_MAX", CONF_HIGH)
        if _VOL_MIN_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "VOLUME_MIN", CONF_HIGH)
        if _VOL_UP_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "VOLUME_UP", CONF_HIGH)
        if _VOL_DN_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "VOLUME_DOWN", CONF_HIGH)
        if _VOL_QRY_RE.search(norm):
            return IntentResult(ROUTE_COMMAND, "VOLUME_QUERY", CONF_HIGH)
        m = _VOL_PCT_RE.search(norm)
        if m and "volume" in norm:
            return IntentResult(ROUTE_COMMAND, "VOLUME_PCT", CONF_HIGH, payload={"pct": int(m.group(1))})
        return None

    # ── Layer 2 ───────────────────────────────────────────────────────────────
    def _layer2_utility(self, norm: str) -> Optional[IntentResult]:
        if _RANDOM_RE.search(norm):
            reply = _random_number_reply(norm)
            return IntentResult(
                ROUTE_UTILITY, "RANDOM_NUMBER", CONF_HIGH,
                response=reply,
                payload={"result": reply.rstrip(".")},
            )
        # Vision before time/date to avoid false matches on "what is this"
        if _VISION_TRIGGERS and any(t in norm for t in _VISION_TRIGGERS):
            return IntentResult(ROUTE_UTILITY, "VISION", CONF_HIGH)
        td = _time_date_reply(norm)
        if td is not None:
            is_t = bool(_TIME_RE.search(norm))
            is_d = bool(_DATE_RE.search(norm))
            action = "TIME_DATE" if (is_t and is_d) else ("TIME" if is_t else "DATE")
            return IntentResult(ROUTE_UTILITY, action, CONF_HIGH, response=td)
        math_result = _parse_math(norm)
        if math_result is not None:
            return IntentResult(
                ROUTE_UTILITY, "MATH", CONF_HIGH,
                response=f"{math_result}.",
                payload={"result": math_result},
            )
        return None

    # ── Layer 3 ───────────────────────────────────────────────────────────────
    def _layer3_ambiguous(self, norm: str, state) -> Optional[IntentResult]:
        if norm in _AMBIGUOUS_STOP_EXACT:
            return IntentResult(ROUTE_AMBIGUOUS, "STOP", CONF_MEDIUM)
        if norm in _AMBIGUOUS_SLEEPY:
            hour = time.localtime().tm_hour
            if hour >= 21 or hour < 8:
                return IntentResult(ROUTE_AMBIGUOUS, "SLEEP", CONF_MEDIUM, response="Goodnight.")
            return IntentResult(ROUTE_AMBIGUOUS, "LLM", CONF_MEDIUM)
        return None
