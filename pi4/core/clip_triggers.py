"""
core/clip_triggers.py - Keyword-based clip trigger map for IRIS quip responses.
check_clip_trigger() is called on the raw STT utterance before LLM routing.

Supports affective variant selection: multiple clips may share the same trigger
keywords. When emotion is provided the clip whose affect list contains that
emotion is preferred. Falls back to the first match if no affect match.

S163: the trigger map is now DATA-DRIVEN. The active clip list comes from
core/soundboard.py (backed by /home/pi/iris_soundboard.json), edited live by the
WebUI Soundboard tab. _CLIPS holds only the *enabled* clips with their trigger
patterns compiled; reload() re-reads after a WebUI save (driven by the assistant
CMD RELOAD_SOUNDBOARD handler). This replaces the S162e hardcoded empty map and
the S159/S160c hardcoded clip literals (now seeded inside soundboard.py, all
enabled=False by default so behavior is unchanged until the operator enables a
clip in the UI).
"""

from __future__ import annotations

import re
from typing import Any

try:
    from core import soundboard
except Exception:  # clip triggers must never crash assistant/web import
    soundboard = None


def _load_clips() -> list[dict[str, Any]]:
    """Return the enabled clips from the soundboard with compiled patterns."""
    if soundboard is None:
        return []
    try:
        clips = soundboard.get_clips(enabled_only=True)
    except Exception as e:
        print(f"[CLIPTRIG] load error: {e}", flush=True)
        return []
    for entry in clips:
        entry["_patterns"] = [
            re.compile(re.escape(kw), re.IGNORECASE)
            for kw in entry.get("triggers", [])
        ]
    return clips


# Active enabled-clip set (compiled). Kept as a module global so iris_web.py's
# `from core.clip_triggers import _CLIPS` keeps working.
_CLIPS: list[dict[str, Any]] = _load_clips()


def reload() -> int:
    """Re-read the enabled clip set after a WebUI soundboard save.
    Returns the number of active clips."""
    global _CLIPS
    _CLIPS = _load_clips()
    return len(_CLIPS)


def check_clip_trigger(utterance: str, emotion: str | None = None) -> str | None:
    """Return a clip filename if the utterance matches any trigger keyword, else None.

    When emotion is provided and multiple clips share a trigger keyword, prefer
    the clip whose affect list contains the emotion. Falls back to the first
    matched clip if no affect match (or emotion is None).

    Preserves existing behavior exactly when emotion=None.
    """
    matches: list[dict] = []
    seen: set[str] = set()
    for entry in _CLIPS:
        if entry["file"] in seen:
            continue
        for pat in entry["_patterns"]:
            if pat.search(utterance):
                matches.append(entry)
                seen.add(entry["file"])
                break
    if not matches:
        return None
    if emotion:
        for entry in matches:
            if emotion in entry.get("affect", []):
                return entry["file"]
    return matches[0]["file"]
