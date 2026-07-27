"""
core/protocol.py - Pi4-side serial protocol contract constants + component
version cache (RD-048 moves 2 + 7, S213).

EXPECTED_*_PROTOCOL are the protocol versions this Pi4 codebase was written
against. Each Teensy firmware bakes its own PROTOCOL_VERSION into its boot
version line ([VER] ... proto=N for the T4.1, "VER servo fw ... proto=N" for
the T4.0). The bridges parse that at connect and LOUD-warn on mismatch --
warn-only, never refuse to connect. Bump the expected value here in the same
commit that bumps the firmware side.

These are code contracts, NOT operator tunables -- deliberately kept out of
core/config.py so they can never be overridden from the WebUI or
iris_config.json.

record_component_version() maintains /tmp/iris_versions.json so iris_web.py
(a separate process) can serve /api/version without grepping the journal.
RD-031 bound: version lines arrive only at Teensy boot or on an explicit
VERSION request, and the file is rewritten only when a value actually changes.
"""

import json
import os
import threading
import time

EXPECTED_EYES_PROTOCOL = 2   # T4.1 (eyes+mouth) -- src/config.h PROTOCOL_VERSION
EXPECTED_SERVO_PROTOCOL = 1  # T4.0 (servo+gesture) -- teensy40_base_mount.ino PROTOCOL_VERSION

VERSIONS_PATH = "/tmp/iris_versions.json"

_lock = threading.Lock()


def record_component_version(component, **fields):
    """Merge {component: fields} into VERSIONS_PATH. Writes only on change
    (RD-031: no unbounded writers). Always stamps seen_ts on the component."""
    fields = dict(fields)
    fields["seen_ts"] = time.time()
    with _lock:
        data = {}
        try:
            with open(VERSIONS_PATH) as f:
                data = json.load(f)
        except Exception:
            data = {}
        prev = dict(data.get(component, {}))
        prev.pop("seen_ts", None)
        cur = dict(fields)
        cur.pop("seen_ts", None)
        # seen_ts refreshes silently with any real change; identical payload
        # still refreshes seen_ts at most once per boot/VERSION request, which
        # is bounded, so just write when payload OR staleness (>60s) changed.
        stale = time.time() - data.get(component, {}).get("seen_ts", 0) > 60
        if prev == cur and not stale:
            return
        data[component] = fields
        tmp = VERSIONS_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, VERSIONS_PATH)
        except Exception as e:
            print(f"[PROTO] version cache write failed: {e}", flush=True)


def read_component_versions():
    """Read the version cache (used by iris_web.py /api/version)."""
    try:
        with open(VERSIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}
