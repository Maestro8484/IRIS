"""
core/viseme_map.py — RD-044 (mouth lip-sync): turn Kokoro per-word timestamps
into a time-ordered mouth-sprite timeline for the TTS playback animator.

The Teensy mouth is a set of 10 baked EMOTION sprites, not phonetic visemes
(0=NEUTRAL 1=HAPPY 2=CURIOUS 3=ANGRY 4=SLEEPY 5=SURPRISED 6=SAD 7=CONFUSED
8=SLEEP/OFF 9=SILLY). The only dimension that reads as "the mouth is speaking"
is openness: NEUTRAL(0) ≈ closed, SURPRISED(5) ≈ wide open. So this module does
NOT attempt phoneme→shape mapping; it drives a jaw-openness ENVELOPE, timed to
real audio, with three realism cues (in priority order):

  1. correct timing        — open on each word's real start_time (Kokoro)
  2. closures on gaps/ends  — mouth closes between words + at the end
  3. intra-word motion      — long words articulate per estimated syllable
                              instead of freezing open

Coarticulation smoothing drops closures shorter than MIN_CLOSE_S so short words
don't machine-gun. Output is a flat list of (time_sec, sprite_idx) events the
player fires by playback position. Pure/stdlib-only so it is unit-testable
offline (see __main__).

build_mouth_timeline(word_timestamps) -> list[(float, int)]
    word_timestamps: iterable of dicts with 'word', 'start_time', 'end_time'
    (exactly Kokoro-FastAPI /dev/captioned_speech 'timestamps' items).
"""

import re

# ── Tunables (retune freely; sprites are emotion faces, so exact idx is taste) ──
MOUTH_CLOSED   = 0     # NEUTRAL — resting/closed jaw between words + at end
MOUTH_OPEN     = 5     # SURPRISED — the one sprite that reads as an open mouth
# Optional mid-open level for a smoother ladder once a good sprite is chosen by
# eye. Set to a sprite idx (e.g. 2=CURIOUS) to enable a 3-level ladder; None =
# proven 2-level open/close pair that is already live today.
MOUTH_MID      = None

OPEN_HOLD_FRAC = 0.50  # fraction of a syllable the jaw stays open before closing
MAX_OPEN_HOLD_S= 0.14  # cap: a long syllable still closes so it re-articulates
MIN_SYL_S      = 0.11  # don't subdivide a word faster than this (per syllable)
GAP_CLOSE_S    = 0.09  # inter-word gap ≥ this → guarantee a closed frame in it
MIN_CLOSE_S    = 0.045 # drop closures shorter than this (~Teensy 55ms render floor)

_VOWEL_GROUP = re.compile(r"[aeiouy]+")
_HAS_ALNUM   = re.compile(r"[a-z0-9]", re.IGNORECASE)


def estimate_syllables(word: str) -> int:
    """Cheap grapheme syllable estimate: count vowel-letter groups, with light
    silent-trailing-'e' correction. Never returns < 1 for a pronounceable word."""
    w = word.lower().strip()
    if not _HAS_ALNUM.search(w):
        return 0
    groups = _VOWEL_GROUP.findall(w)
    n = len(groups)
    # silent trailing 'e' ("make", "large") — but not "the"/"be"/"he" (n would be 1)
    if n >= 2 and w.endswith("e") and not w.endswith(("le", "ee")):
        n -= 1
    return max(1, n)


def build_mouth_timeline(word_timestamps):
    """Expand Kokoro word timestamps into a sorted [(t_sec, sprite_idx)] timeline.

    Returns [] if there is nothing usable (caller then falls back to the legacy
    fixed-timer animation for that utterance)."""
    words = []
    for tok in (word_timestamps or []):
        try:
            w = str(tok.get("word", ""))
            s = float(tok.get("start_time"))
            e = float(tok.get("end_time"))
        except (TypeError, ValueError, AttributeError):
            continue
        if e <= s:
            continue
        words.append((w, s, e))
    if not words:
        return []

    raw = [(0.0, MOUTH_CLOSED)]  # start closed so entry from rest never pops open
    for i, (w, s, e) in enumerate(words):
        # Punctuation / whitespace tokens carry timing but no shape → stay closed.
        if not _HAS_ALNUM.search(w):
            raw.append((s, MOUTH_CLOSED))
            continue
        dur = e - s
        syl = estimate_syllables(w)
        # never articulate faster than MIN_SYL_S
        syl = max(1, min(syl, int(dur / MIN_SYL_S) or 1))
        per = dur / syl
        for k in range(syl):
            t_open = s + k * per
            raw.append((t_open, MOUTH_OPEN))
            t_close = t_open + min(per * OPEN_HOLD_FRAC, MAX_OPEN_HOLD_S)
            if t_close < e - 0.005:
                raw.append((t_close, MOUTH_MID if MOUTH_MID is not None else MOUTH_CLOSED))
        # Close in the gap before the next word (or at utterance end).
        nxt = words[i + 1][1] if i + 1 < len(words) else None
        if nxt is None or (nxt - e) >= GAP_CLOSE_S:
            raw.append((e, MOUTH_CLOSED))

    raw.append((words[-1][2] + 0.02, MOUTH_CLOSED))  # guarantee a closed tail
    raw.sort(key=lambda ev: ev[0])

    # ── Smoothing pass ──────────────────────────────────────────────────────
    # (a) drop a CLOSED/MID that is immediately followed (< MIN_CLOSE_S) by an
    #     OPEN — too short to see, would just flicker (Teensy coalesces anyway).
    # (b) collapse consecutive events with the same sprite idx.
    smoothed = []
    for j, (t, idx) in enumerate(raw):
        if idx != MOUTH_OPEN:
            # look ahead for the next event; if it's an OPEN arriving too soon,
            # skip this closure/mid so the jaw stays open through the run.
            nt = raw[j + 1][0] if j + 1 < len(raw) else None
            ni = raw[j + 1][1] if j + 1 < len(raw) else None
            if nt is not None and ni == MOUTH_OPEN and (nt - t) < MIN_CLOSE_S:
                continue
        if smoothed and smoothed[-1][1] == idx:
            continue
        smoothed.append((round(t, 4), idx))

    # guarantee the timeline starts closed (the smoother may have dropped the
    # t=0 seed if the first word opens < MIN_CLOSE_S later) and ends closed, so
    # the player's rest→speaking→rest boundary never pops open or sticks open.
    if not smoothed or smoothed[0] != (0.0, MOUTH_CLOSED):
        smoothed.insert(0, (0.0, MOUTH_CLOSED))
    if smoothed[-1][1] != MOUTH_CLOSED:
        smoothed.append((round(words[-1][2] + 0.02, 4), MOUTH_CLOSED))
    return smoothed


if __name__ == "__main__":
    # Self-test against a REAL Kokoro /dev/captioned_speech response captured
    # live S189 for "Hello there, I am Iris." (voice bf_lily(0.8)+bf_emma(0.2)).
    sample = [
        {"word": "Hello", "start_time": 0.0535, "end_time": 0.4785},
        {"word": "there", "start_time": 0.4785, "end_time": 0.7535},
        {"word": ",",     "start_time": 0.7535, "end_time": 0.8285},
        {"word": "I",     "start_time": 0.8285, "end_time": 1.0035},
        {"word": "am",    "start_time": 1.0035, "end_time": 1.166},
        {"word": "Iris",  "start_time": 1.166,  "end_time": 2.0035},
        {"word": ".",     "start_time": 2.0035, "end_time": 2.2035},
    ]
    tl = build_mouth_timeline(sample)
    print(f"syllables: Hello={estimate_syllables('Hello')} there={estimate_syllables('there')} "
          f"I={estimate_syllables('I')} am={estimate_syllables('am')} Iris={estimate_syllables('Iris')}")
    print(f"timeline ({len(tl)} events):")
    for t, idx in tl:
        name = {0: "CLOSED", 5: "OPEN", 2: "MID"}.get(idx, str(idx))
        print(f"  t={t:6.3f}  {name}")
    assert tl[0] == (0.0, MOUTH_CLOSED), "must start closed"
    assert tl[-1][1] == MOUTH_CLOSED, "must end closed"
    print("OK")
