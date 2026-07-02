"""
soundboard_api.py — Flask blueprint for the WebUI Quip + Clip (Soundboard) manager.

Self-contained strangler module: imported and registered by iris_web.py with a
single line. Owns the /api/soundboard/* routes and serves the tab's JS. The data
layer (core/soundboard.py) is the source of truth; this blueprint reads it fresh
per request (cross-process safe) and, on save, writes RAM+SD atomically then
pings the assistant's CMD UDP listener so the live process reloads clips and
re-synthesizes any changed quip lines WITHOUT a service restart.

Reuses the existing /api/clips/upload + /api/clips/play endpoints (iris_web.py)
for clip file upload/preview — no duplication.
"""
from __future__ import annotations

import os
import re
import socket

from flask import Blueprint, request, jsonify

from core import soundboard as sb
from core.config import CMD_PORT

soundboard_bp = Blueprint("soundboard", __name__)

_WEB_DIR  = os.path.dirname(os.path.abspath(__file__))
_JS_FILE  = os.path.join(_WEB_DIR, "iris_web_soundboard.js")
CLIPS_DIR = "/home/pi/clips"


def _notify_reload() -> bool:
    """Tell the assistant process to reload the soundboard (clips + quip resynth).
    Best-effort UDP to the existing CMD listener; never raises."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"RELOAD_SOUNDBOARD", ("127.0.0.1", CMD_PORT))
        return True
    except Exception as e:
        print(f"[SOUNDBOARD-API] reload notify failed: {e}", flush=True)
        return False


def _disk_clips() -> list:
    """WAV filenames present on disk in CLIPS_DIR (so the UI can flag clips whose
    WAV is missing, and offer disk files not yet in the data model)."""
    try:
        return sorted(fn for fn in os.listdir(CLIPS_DIR) if fn.lower().endswith(".wav"))
    except OSError:
        return []


@soundboard_bp.route("/soundboard.js")
def soundboard_js():
    with open(_JS_FILE, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "application/javascript; charset=utf-8"}


@soundboard_bp.route("/api/soundboard")
def api_soundboard_get():
    """Full validated soundboard state + the WAV files actually on disk."""
    try:
        data = sb.load(force=True)
        return jsonify(
            ok=True,
            version=data.get("version", sb.SCHEMA_VERSION),
            clips=data.get("clips", []),
            quips=data.get("quips", {}),
            disk_clips=_disk_clips(),
            valid_emotions=sorted(sb.VALID_EMOTIONS),
            gesture_keys=list(sb.GESTURE_CUE_KEYS),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@soundboard_bp.route("/api/soundboard/save", methods=["POST"])
def api_soundboard_save():
    """Replace the whole soundboard document. Body: {clips:[...], quips:{...}}.
    Validated + normalized by core.soundboard.save (drops bad fields, backfills
    missing categories), atomically written RAM+SD, then the assistant is told to
    reload. Returns ok plus the SD-persist result so the UI can warn if the SD
    write did not verify (RAM-only = lost on reboot)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="expected a JSON object"), 400
    try:
        result = sb.save({"clips": body.get("clips"), "quips": body.get("quips")})
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    reloaded = _notify_reload()
    return jsonify(ok=True, md5=result.get("md5"), sd=result.get("sd"),
                   version=result.get("version"), reloaded=reloaded)


@soundboard_bp.route("/api/soundboard/reset", methods=["POST"])
def api_soundboard_reset():
    """Restore to seed defaults (all clips disabled, original quips). Current
    state is snapshotted to iris_soundboard.json.goldbak before overwrite."""
    try:
        result = sb.reset_to_default()
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    reloaded = _notify_reload()
    return jsonify(ok=True, md5=result.get("md5"), sd=result.get("sd"),
                   reloaded=reloaded)


@soundboard_bp.route("/api/soundboard/restore", methods=["POST"])
def api_soundboard_restore():
    """Undo the last save: restore the .goldbak snapshot (state just before the
    most recent save) to RAM+SD, then tell the assistant to reload."""
    try:
        result = sb.restore_goldbak()
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error", "restore failed")), 400
    reloaded = _notify_reload()
    return jsonify(ok=True, md5=result.get("md5"), sd=result.get("sd"),
                   version=result.get("version"), reloaded=reloaded)


@soundboard_bp.route("/api/soundboard/test")
def api_soundboard_test():
    """Return which clip would fire for a given utterance + optional emotion.
    Replicates check_clip_trigger logic against the live enabled-clips list
    (fresh read — reflects any unsaved edits only after a save)."""
    utterance = request.args.get("u", "").strip()
    emotion = request.args.get("emotion", "").strip().upper() or None
    if not utterance:
        return jsonify(ok=False, error="u param required"), 400
    if emotion and emotion not in sb.VALID_EMOTIONS:
        emotion = None
    clips = sb.get_clips(enabled_only=True)
    matches: list[dict] = []
    seen: set[str] = set()
    for c in clips:
        fn = c["file"]
        if fn in seen:
            continue
        for kw in c.get("triggers", []):
            if re.search(re.escape(kw), utterance, re.IGNORECASE):
                matches.append(c)
                seen.add(fn)
                break
    result = None
    if matches:
        if emotion:
            for c in matches:
                if emotion in c.get("affect", []):
                    result = c["file"]
                    break
        if result is None:
            result = matches[0]["file"]
    return jsonify(ok=True, match=result,
                   all_matches=[c["file"] for c in matches],
                   utterance=utterance, emotion=emotion)
