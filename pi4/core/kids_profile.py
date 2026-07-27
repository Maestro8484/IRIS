"""
core/kids_profile.py — Data-driven source of truth for who the children are.

A single JSON file (/home/pi/kids_profile.json, SD-mirrored) holds each child's
name, age, and interests. assistant.py folds it into the kids-mode turn so IRIS
is specifically interested in THESE two children rather than children in general.
Editing it is a pure WebUI/data action — no code edit, no redeploy, no restart
(the WebUI POST sends UDP RELOAD_KIDS_PROFILE).

This is literally the "little notebook Dad keeps" that the iris-kids persona
tells the children about. Keeping that claim true is the point of the module.

The word is INTERESTS, not obsessions. It appears in the JSON key, the WebUI
label, and the injected prompt text. Operator was explicit (RD-047).

Structure, caps, validation, mtime cache, atomic dual-write RAM+SD with md5
verification, and the .goldbak snapshot all mirror core/soundboard.py (S163) —
including the rule that it never touches the api_persist_config mount sequence
(S152 / RD-034).

INJECTION CONSTRAINT: the profile is folded into the CURRENT USER TURN, never
sent as a {"role": "system"} message. In Ollama a request system message
OVERRIDES the modelfile SYSTEM and strips the persona entirely (S134 regression,
CHANGELOG.md:3156). See assistant._build_messages().
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import threading

DATA_FILE    = "/home/pi/kids_profile.json"
SD_DATA_FILE = "/media/root-ro/home/pi/kids_profile.json"

_GOLDBAK_FILE    = DATA_FILE + ".goldbak"
_SD_GOLDBAK_FILE = SD_DATA_FILE + ".goldbak"

SCHEMA_VERSION = 1

# Resource caps — bound the injected prompt so it can never eat num_ctx (6144)
# or let a WebUI paste blow up the turn. RD-031 spirit.
_MAX_CHILDREN       = 8
_MAX_INTERESTS      = 12
_MAX_NAME_CHARS     = 40
_MAX_INTEREST_CHARS = 200
_MIN_AGE, _MAX_AGE  = 0, 21

_DEFAULT_CHILDREN = [
    {
        "name": "Ava",
        "age": 6,
        "interests": [
            "reading and scanning Ben's graphic novels",
            "caring for her babies (they are babies, not toys)",
            "horses, and her own real horse at their grandparents' house nearby",
            "swimming in the new pool",
        ],
    },
    {
        "name": "Ben",
        "age": 9,
        "interests": [
            "friends, and talking and texting them on his mobile-watch",
            "lacrosse",
            "graphic novels: Dog Man, Bad Guys, Diary of a Wimpy Kid",
        ],
    },
]

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = -1.0


def _default() -> dict:
    """A fresh, mutable copy of the seed."""
    return {
        "version": SCHEMA_VERSION,
        "children": [
            {"name": c["name"], "age": c["age"], "interests": list(c["interests"])}
            for c in _DEFAULT_CHILDREN
        ],
    }


def _clean_interests(seq) -> list:
    if not isinstance(seq, list):
        return []
    out = []
    for s in seq:
        if isinstance(s, str) and s.strip():
            out.append(s.strip()[:_MAX_INTEREST_CHARS])
        if len(out) >= _MAX_INTERESTS:
            break
    return out


def validate(data: dict) -> dict:
    """Return a normalized, structurally-safe copy. Tolerant: drops bad fields,
    falls back to DEFAULT when nothing usable survives — never raises on partial
    data, so a hand-edited or older file can't crash the assistant at import."""
    out = _default()
    if not isinstance(data, dict):
        return out

    kids = data.get("children")
    if not isinstance(kids, list):
        return out

    norm, seen = [], set()
    for c in kids:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()[:_MAX_NAME_CHARS]
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            age = int(c.get("age"))
        except (TypeError, ValueError):
            age = None
        if age is None or not (_MIN_AGE <= age <= _MAX_AGE):
            age = None
        norm.append({
            "name": name,
            "age": age,
            "interests": _clean_interests(c.get("interests")),
        })
        if len(norm) >= _MAX_CHILDREN:
            break

    # An empty/garbage children list means "no profile", not "wipe the seed".
    if norm:
        out["children"] = norm
    try:
        out["version"] = int(data.get("version", SCHEMA_VERSION))
    except (TypeError, ValueError):
        out["version"] = SCHEMA_VERSION
    return out


# ── Load / cache ──────────────────────────────────────────────────────────────

def _read_file() -> dict | None:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[KIDPROF] read error: {e}", flush=True)
        return None


def load(force: bool = False) -> dict:
    """Current profile (validated). Cached by file mtime so cross-process readers
    pick up WebUI edits on next access; force=True bypasses the cache."""
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(DATA_FILE)
        except OSError:
            mtime = -1.0
        if force or _cache is None or mtime != _cache_mtime:
            raw = _read_file()
            if raw is None:
                data = _default()
                try:
                    _write_ram(data)
                    mtime = os.path.getmtime(DATA_FILE)
                except Exception as e:
                    print(f"[KIDPROF] seed write failed: {e}", flush=True)
            else:
                data = validate(raw)
            _cache = data
            _cache_mtime = mtime
        return _cache


def reload() -> dict:
    """Force a fresh read; used by the assistant CMD RELOAD_KIDS_PROFILE handler."""
    return load(force=True)


def get_children() -> list:
    return [dict(c, interests=list(c["interests"])) for c in load().get("children", [])]


def prompt_context(recall_records=None) -> str:
    """The kids-mode context clause(s), ready to be prepended to the current user
    turn. Returns "" when there is nothing useful to inject.

    Two parts, both "(..., not spoken: ...)" shaped to match the date stamp
    assistant._build_messages() already folds in, which the persona is told to
    expect. NEVER pass this as a role:system message (S134):
      1. WHO the children are  — the profile (this module's notebook).
      2. WHAT they've been on about lately — recurring-topic/recent-event recall
         (core.kids_recall, S201B). Name-BLIND: it never says which child said it.

    recall_records:
      None (production, assistant.py's no-arg call) -> recall is read from the live
        conversations.jsonl (mtime-cached).
      list of records (persona harness) -> recall is computed from those scripted
        conversations, so the callback can be asserted deterministically offline.
    Recall is additive and fully guarded: any failure leaves the profile untouched.
    """
    kids = load().get("children", [])
    parts = []
    for c in kids:
        name = c.get("name")
        if not name:
            continue
        age = c.get("age")
        who = f"{name} ({age})" if age is not None else name
        interests = c.get("interests") or []
        if interests:
            parts.append(f"{who} is interested in " + "; ".join(interests))
        else:
            parts.append(f"{who} lives here")

    profile_clause = "(Context, not spoken: " + ". ".join(parts) + ".)" if parts else ""

    recall_clause = ""
    try:
        from core import kids_recall
        if recall_records is None:
            recall_clause = kids_recall.clause_from_log()
        else:
            recall_clause = kids_recall.recall_clause(recall_records)
    except Exception as _re:
        print(f"[KIDPROF] recall skipped: {_re}", flush=True)
        recall_clause = ""

    return " ".join(p for p in (profile_clause, recall_clause) if p)


# ── Save (atomic dual-write, S158/S163 pattern) ───────────────────────────────

def _write_ram(data: dict) -> str:
    """Atomic RAM write via tmp -> md5 -> os.replace(). Returns md5 hex."""
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    md5 = hashlib.md5(payload).hexdigest()
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(payload)
    with open(tmp, "rb") as f:
        if hashlib.md5(f.read()).hexdigest() != md5:
            try: os.unlink(tmp)
            except OSError: pass
            raise IOError("RAM write md5 mismatch")
    os.replace(tmp, DATA_FILE)
    return md5


def save(data: dict, persist_sd: bool = True) -> dict:
    """Validate + atomically write. RAM always; SD mirror with md5 RAM==SD
    verification unless persist_sd=False. Snapshots the previous RAM file to
    .goldbak first. Returns {ok, md5, sd, version}."""
    global _cache, _cache_mtime
    norm = validate(data)
    with _lock:
        current_ver = _cache.get("version", SCHEMA_VERSION) if _cache else SCHEMA_VERSION
        norm["version"] = current_ver + 1
        try:
            with open(DATA_FILE, "rb") as f_src:
                bak_bytes = f_src.read()
            with open(_GOLDBAK_FILE, "wb") as f_dst:
                f_dst.write(bak_bytes)
        except (FileNotFoundError, IOError):
            pass  # no previous file yet
        md5 = _write_ram(norm)
        _cache = norm
        try:
            _cache_mtime = os.path.getmtime(DATA_FILE)
        except OSError:
            _cache_mtime = -1.0

    sd_ok = None
    if persist_sd:
        sd_ok = _persist_sd()
    return {"ok": True, "md5": md5, "sd": sd_ok, "version": norm["version"]}


def reset_to_default() -> dict:
    """Write seed defaults to RAM+SD. Current state is snapshotted by save()."""
    return save(_default(), persist_sd=True)


def restore_goldbak() -> dict:
    """Undo the last save by restoring the .goldbak snapshot to RAM+SD."""
    try:
        with open(_GOLDBAK_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"ok": False, "error": "no goldbak snapshot yet (save at least once)"}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": f"unreadable goldbak: {e}"}
    return save(validate(raw), persist_sd=True)


def _persist_sd() -> bool:
    """Copy RAM file to the SD overlay (remount,rw -> cp -> sync -> remount,ro),
    then verify md5 RAM==SD. Also writes the SD goldbak. Does NOT touch the
    api_persist_config mount sequence (S152 / RD-034)."""
    q_ram        = shlex.quote(DATA_FILE)
    q_sd         = shlex.quote(SD_DATA_FILE)
    q_sd_dir     = shlex.quote(os.path.dirname(SD_DATA_FILE))
    q_sd_goldbak = shlex.quote(_SD_GOLDBAK_FILE)
    try:
        r = subprocess.run(
            ["sudo", "bash", "-c",
             f"mkdir -p {q_sd_dir} && "
             f"mount -o remount,rw /media/root-ro && "
             f"{{ cp {q_sd} {q_sd_goldbak} 2>/dev/null; true; }} && "
             f"cp {q_ram} {q_sd} && "
             f"chmod 644 {q_sd} && "
             f"{{ chmod 644 {q_sd_goldbak} 2>/dev/null; true; }} && "
             f"sync && "
             f"mount -o remount,ro /media/root-ro"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print(f"[KIDPROF] SD persist failed: {r.stderr.strip()}", flush=True)
            return False
    except Exception as e:
        print(f"[KIDPROF] SD persist error: {e}", flush=True)
        return False

    try:
        with open(DATA_FILE, "rb") as f:
            ram_md5 = hashlib.md5(f.read()).hexdigest()
        r = subprocess.run(["sudo", "md5sum", SD_DATA_FILE],
                           capture_output=True, text=True, timeout=10)
        sd_md5 = r.stdout.split()[0] if r.returncode == 0 and r.stdout else ""
    except Exception as e:
        print(f"[KIDPROF] SD md5 verify error: {e}", flush=True)
        return False

    if ram_md5 != sd_md5:
        print(f"[KIDPROF] SD md5 MISMATCH ram={ram_md5} sd={sd_md5}", flush=True)
        return False
    return True
