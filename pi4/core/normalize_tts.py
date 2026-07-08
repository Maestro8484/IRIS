"""
core/normalize_tts.py - bench-proven TTS text normalization for Kokoro.

Replaces the old blind `spoken_numbers()` digit substitution. The S194 Kokoro
language audit (docs/S194_kokoro_language_audit.md) benched live Kokoro against
23 strings sent RAW vs through the pipeline and found the old pipeline actively
*regressed* several classes Kokoro already handles natively (decimals, negatives,
years, $/£ currency, H:MM times) by blindly substituting bare digits inside
patterns it did not recognize as a unit -- while genuinely broken classes
(fractions, x/* multiplication, H:MM colon token, dash codes/ranges) went
unfixed.

`normalize_for_tts()` implements exactly the audit's ranked spec (§"Recommended
normalize_for_tts() spec", rules 1-13). ORDER MATTERS: every specific pattern
claims its digits BEFORE the generic bare-integer fallback, and everything runs
BEFORE the `[^\x00-\x7F]` non-ASCII strip in services.tts._clean_tts_text -- so
protected non-ASCII content (£, °, —) is turned into ASCII words here rather than
being silently deleted downstream. Ordinals (1st/2nd/3rd) and bare integers Kokoro
already reads well are passed through / handled last, unchanged.

Pure function, stdlib `re` only. Output is ASCII-only for every audit matrix input.
"""

import re

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty",
         "sixty", "seventy", "eighty", "ninety"]

# Common cooking fractions -> natural words (audit rule 6). Anything else -> "N over M".
_FRACTIONS = {
    (1, 2): "one half",   (1, 3): "one third",  (2, 3): "two thirds",
    (1, 4): "one quarter", (3, 4): "three quarters",
}


def _int_to_words(n: int) -> str:
    if n < 0:
        return "minus " + _int_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        rest = (" " + _ONES[n % 10]) if n % 10 else ""
        return _TENS[n // 10] + rest
    if n < 1000:
        rest = (" " + _int_to_words(n % 100)) if n % 100 else ""
        return _ONES[n // 100] + " hundred" + rest
    if n < 1_000_000:
        thousands = n // 1000
        remainder = n % 1000
        rest = (" " + _int_to_words(remainder)) if remainder else ""
        return _int_to_words(thousands) + " thousand" + rest
    if n < 1_000_000_000:
        millions = n // 1_000_000
        remainder = n % 1_000_000
        rest = (" " + _int_to_words(remainder)) if remainder else ""
        return _int_to_words(millions) + " million" + rest
    return str(n)


# ── Per-rule substitution helpers ─────────────────────────────────────────────

def _currency(m: "re.Match") -> str:
    sym, whole, frac = m.group(1), m.group(2), m.group(3)
    major = "dollars" if sym == "$" else "pounds"
    minor = "cents" if sym == "$" else "pence"
    out = _int_to_words(int(whole)) + " " + major
    if frac and int(frac):
        out += " and " + _int_to_words(int(frac)) + " " + minor
    return out


def _time(m: "re.Match") -> str:
    h, mm = int(m.group(1)), int(m.group(2))
    hw = _int_to_words(h)
    if mm == 0:
        return hw + " o'clock"
    if mm < 10:
        return hw + " oh " + _int_to_words(mm)
    return hw + " " + _int_to_words(mm)


def _code(m: "re.Match") -> str:
    return ", ".join(_int_to_words(int(p)) for p in m.group(0).split("-"))


def _dash_range(m: "re.Match") -> str:
    return _int_to_words(int(m.group(1))) + " to " + _int_to_words(int(m.group(2)))


def _decimal(m: "re.Match") -> str:
    intpart, frac = m.group(1), m.group(2)
    neg = intpart.startswith("-")
    words = ("minus " if neg else "") + _int_to_words(int(intpart.lstrip("-")))
    frac_words = " ".join(_ONES[int(d)] for d in frac)
    return words + " point " + frac_words


def _year(m: "re.Match") -> str:
    y = m.group(0)
    first2, last2 = int(y[:2]), int(y[2:])
    if last2 == 0:
        if int(y) % 1000 == 0:            # 2000 -> "two thousand"
            return _int_to_words(int(y) // 1000) + " thousand"
        return _int_to_words(first2) + " hundred"   # 1900 -> "nineteen hundred"
    if last2 < 10:                        # 2006 -> "twenty oh six"
        return _int_to_words(first2) + " oh " + _int_to_words(last2)
    return _int_to_words(first2) + " " + _int_to_words(last2)  # 2026 -> "twenty twenty six"


def _fraction(m: "re.Match") -> str:
    a, b = int(m.group(1)), int(m.group(2))
    if (a, b) in _FRACTIONS:
        return _FRACTIONS[(a, b)]
    return _int_to_words(a) + " over " + _int_to_words(b)


def normalize_for_tts(text: str) -> str:
    """Normalize numeric/symbolic tokens for natural Kokoro TTS.

    Implements docs/S194_kokoro_language_audit.md rules 1-13 in the mandated
    order (specific patterns first, generic bare-integer fallback last, all
    before the downstream non-ASCII strip).

    Converts every number/prosody glyph Kokoro needs (incl. the Unicode ellipsis)
    into ASCII words/marks. Any *other* residual non-ASCII (accents, emoji, smart
    quotes) is left for services.tts._clean_tts_text's `[^\\x00-\\x7F]` strip -- so
    this function does not guarantee fully ASCII output for arbitrary input.
    """
    # 0. Unicode ellipsis (…, U+2026) -> ASCII "..." BEFORE the downstream non-ASCII
    #    strip would delete it. Live S194 bench: "..." is Kokoro's longest pause cue,
    #    so the glyph must survive as ASCII dots or the pause is silently lost.
    text = text.replace('…', '...')

    # 1. Currency ($, £) -- consume symbol + optional pence/cents BEFORE decimals
    #    (so "£3.50" is one unit) and before the generic rule / ASCII strip.
    text = re.sub(r'([$£])(\d+)(?:\.(\d{2}))?', _currency, text)

    # 2. Times (H:MM) -- before the generic rule so the colon never survives.
    text = re.sub(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', _time, text)

    # 3. Dash codes (3+ groups, e.g. 4-7-2-1) then simple 2-group ranges (5-10).
    #    Codes first so a range regex can't eat part of a code. Both before the
    #    negative-number rule and the generic rule (audit rule 8).
    text = re.sub(r'\b\d{1,2}(?:-\d{1,2}){2,}\b', _code, text)
    text = re.sub(r'\b(\d+)-(\d+)\b', _dash_range, text)

    # 3b. Multi-part dotted numbers (versions "1.2.3", IPs "192.168.1.1") -- BEFORE
    #     the 2-group decimal rule, which would otherwise claim only the first pair
    #     and garble the rest into "one point two.three". Read each group in order,
    #     joined by "point".
    text = re.sub(
        r'\b\d+(?:\.\d+){2,}\b',
        lambda m: " point ".join(_int_to_words(int(p)) for p in m.group(0).split(".")),
        text,
    )

    # 4. Decimals -- before negatives and before the generic rule, else the dot
    #    boundary lets the generic rule fire on each side (audit rule 1).
    text = re.sub(r'(-?\d+)\.(\d+)', _decimal, text)

    # 5. Negative numbers -- hyphen preceded by start/space/currency, not a digit
    #    or another dash (dash-ranges already consumed above) (audit rule 2).
    text = re.sub(r'(?<![\d-])-(\d+)\b',
                  lambda m: "minus " + _int_to_words(int(m.group(1))), text)

    # 6. Years (1500-2099) -> paired reading -- before the generic thousands rule.
    #    Not preceded by $/£/decimal point (those already consumed above).
    text = re.sub(r'(?<![.$£\d])\b(1[5-9]\d{2}|20\d{2})\b', _year, text)

    # 7. Fractions (N/M) -- before the generic rule (audit rule 6).
    text = re.sub(r'\b(\d+)/(\d+)\b', _fraction, text)

    # 8. Multiplication (x / *) between digits -> " times " -- before the generic
    #    rule so the digits on either side are still intact for the lookarounds.
    text = re.sub(r'(?<=\d)\s*[xX*]\s*(?=\d)', ' times ', text)

    # 9. Existing symbol-suffixed patterns (°F / mph / %) -- kept as-is, run ahead
    #    of the generic rule and the ASCII strip so °F is consumed not deleted.
    text = re.sub(r'(\d+)\s*[°º]?F\b',
                  lambda m: _int_to_words(int(m.group(1))) + " degrees", text)
    text = re.sub(r'(\d+)\s*mph\b',
                  lambda m: _int_to_words(int(m.group(1))) + " miles per hour",
                  text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+)\s*%',
                  lambda m: _int_to_words(int(m.group(1))) + " percent", text)

    # 10. Em-dash / en-dash -> ", " (comma prosody) before the ASCII strip would
    #     otherwise silently delete the glyph (audit rule 11).
    text = re.sub(r'\s*[—–]\s*', ', ', text)

    # 11. Bare integers -- generic fallback LAST, after every pattern above has
    #     had first claim on its digits. \b...\b leaves ordinals (1st) untouched.
    text = re.sub(r'\b(\d+)\b', lambda m: _int_to_words(int(m.group(1))), text)

    return text
