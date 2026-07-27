#!/usr/bin/env python3
"""
iris_corpus_drain.py - ship queued IRIS conversations off-box (S224a, Phase A).

DEPLOY TARGET: Pi4  /home/pi/scripts/iris_corpus_drain.py

THE DEFECT THIS CLOSES (measured 2026-07-21): /media/root-rw is tmpfs and
/media/root-ro (the SD) is mounted read-only, so anything the Pi writes at runtime
lives in RAM and dies at power-off. /home/pi/logs/conversations.jsonl did not
survive the morning power cycle and was gone from both RAM and SD.

THE SHAPE: the Pi is a QUEUE, not a store. assistant.flush_conversation_log()
drops one record per file into OUTBOX. This drain POSTs each one to the corpus
server on GandalfAI and deletes it only after an ack. Steady state on the Pi is
zero bytes. The store of record is C:\\IRIS\\corpus on GandalfAI (scripts/
iris_corpus_server.py).

WHY THERE IS AN SD LEG AT ALL: GandalfAI sleeps by design, so delivery cannot be
guaranteed at the instant of the write. Anything still unacked at the end of a
pass is mirrored to the SD so a reboot during a GandalfAI sleep does not lose it;
the overlay lowerdir makes those files reappear at the RAM path on the next boot,
so there is no restore step. Acked records are deleted from the SD FIRST and the
RAM copy second: deleting through the overlay alone would only write a whiteout,
and the SD copy would resurrect on reboot as a duplicate.

In steady state (queue drains) the SD is never touched and never remounted.

Runs every 15 min, invoked at the tail of /home/pi/scripts/iris_log_export.sh.

  python3 /home/pi/scripts/iris_corpus_drain.py            # one drain pass
  python3 /home/pi/scripts/iris_corpus_drain.py --selftest # offline, no net, no SD
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

OUTBOX     = "/home/pi/logs/outbox"
SD_OUTBOX  = "/media/root-ro/home/pi/logs/outbox"
SD_MOUNT   = "/media/root-ro"
# PORT MOVED 8006 -> 8021 at RD-057, in lockstep with core/config.py RECALL_URL
# and scripts/iris_corpus_server.py. THIS FILE IS THE SECOND PRODUCTION CALLER of
# that endpoint and it is the easy one to forget: recall is the visible half, so a
# port move that only fixes core/config.py looks like a complete success while the
# drain quietly stops shipping and the outbox fills instead. Steady state is zero
# files in OUTBOX -- if it is not zero, this URL or the secret is wrong.
INGEST_URL = "http://192.168.0.20:8021/ingest"
# Shared secret (RD-057), sent as X-IRIS-Auth. Absent file = no header, which is
# the pre-RD-057 behavior; the server decides whether to serve that.
SECRET_FILE = "/home/pi/corpus_secret.txt"
AUTH_HEADER = "X-IRIS-Auth"
TIMEOUT_S  = 8

# Bound (RD-031). Steady state is 0 files. This is the failure-mode ceiling for a
# long GandalfAI outage: ~500 conversations is ~25 days at the measured ~20/day,
# ~2 MB at the measured ~4 KB/record. Beyond it the OLDEST are dropped, loudly.
MAX_QUEUE = 500


def _log(msg):
    print("[DRAIN] %s" % msg, flush=True)


def queued_files(outbox=OUTBOX):
    """Oldest first, by filename (names are timestamp-prefixed and sort correctly)."""
    try:
        return sorted(f for f in os.listdir(outbox) if f.endswith(".json"))
    except FileNotFoundError:
        return []


def enforce_bound(names, max_queue=MAX_QUEUE):
    """Return (keep, drop). Drops the OLDEST beyond the cap, never the newest."""
    if len(names) <= max_queue:
        return names, []
    cut = len(names) - max_queue
    return names[cut:], names[:cut]


def read_secret(path=SECRET_FILE):
    """Read the shared corpus secret. "" when there is none (not an error)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return ""


def post_record(name, record, url=INGEST_URL, timeout=TIMEOUT_S, opener=None,
                secret=None):
    """POST one record. Returns True only on an explicit ok ack.

    An auth failure returns False exactly like a sleeping GandalfAI, so the record
    STAYS QUEUED and is retried rather than lost. That is the correct failure mode
    for a bad secret: nothing is dropped, the outbox grows, and the growth is the
    signal that the two sides disagree.
    """
    body = json.dumps({"id": os.path.splitext(name)[0], "record": record}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    sec = read_secret() if secret is None else secret
    if sec:
        headers[AUTH_HEADER] = sec
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        send = opener or urllib.request.urlopen
        with send(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def _mounted_mode(lines=None):
    """Read the real state from /proc/mounts. 'ro', 'rw', or None if not mounted.
    `lines` is injectable so the selftest can exercise the parse with no /proc."""
    try:
        if lines is None:
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                lines = f.readlines()
        for line in lines:
            parts = line.split()
            if len(parts) > 3 and parts[1] == SD_MOUNT:
                return "ro" if parts[3].split(",")[0] == "ro" else "rw"
    except OSError:
        pass
    return None


def _remount(mode, attempts=4):
    """Remount the SD and VERIFY it took. A remount can transiently fail with EBUSY
    right after writes, and a silently swallowed failure is how the card gets left
    writable (the S221 /api/persist_config class of bug). Never assume, check."""
    for i in range(attempts):
        subprocess.run(["sudo", "mount", "-o", "remount,%s" % mode, SD_MOUNT],
                       check=False, capture_output=True)
        if _mounted_mode() == mode:
            return True
        subprocess.run(["sync"], check=False)
        time.sleep(0.4 * (i + 1))
    _log("REMOUNT %s FAILED after %d attempts - card is %s"
         % (mode.upper(), attempts, _mounted_mode()))
    return False


def _sd_reconcile(acked, still_queued):
    """One rw window: drop acked from SD, mirror unacked to SD. No-op if nothing to do."""
    need_delete = [n for n in acked if os.path.exists(os.path.join(SD_OUTBOX, n))]
    need_mirror = [n for n in still_queued
                   if not os.path.exists(os.path.join(SD_OUTBOX, n))]
    if not need_delete and not need_mirror:
        return 0, 0
    if not _remount("rw"):
        # Could not open the window. Leave everything queued and try next pass;
        # never write blind, and never leave the card in an unknown state.
        _remount("ro")
        return 0, 0
    try:
        os.makedirs(SD_OUTBOX, exist_ok=True)
        try:
            shutil.chown(SD_OUTBOX, "pi", "pi")
        except (LookupError, PermissionError, OSError):
            pass
        for n in need_delete:
            try:
                os.remove(os.path.join(SD_OUTBOX, n))
            except OSError as e:
                _log("SD delete failed %s: %s" % (n, e))
        for n in need_mirror:
            src, dst = os.path.join(OUTBOX, n), os.path.join(SD_OUTBOX, n)
            try:
                shutil.copyfile(src, dst + ".tmp")
                os.replace(dst + ".tmp", dst)
                try:
                    shutil.chown(dst, "pi", "pi")
                except (LookupError, PermissionError, OSError):
                    pass
            except OSError as e:
                _log("SD mirror failed %s: %s" % (n, e))
        subprocess.run(["sync"], check=False)
    finally:
        # Always hand the card back read-only, even if the above raised.
        _remount("ro")
    return len(need_delete), len(need_mirror)


def drain(outbox=OUTBOX):
    names = queued_files(outbox)
    if not names:
        return 0, 0
    names, dropped = enforce_bound(names)
    for n in dropped:
        _log("QUEUE OVER CAP (%d) - dropping oldest: %s" % (MAX_QUEUE, n))
        for path in (os.path.join(SD_OUTBOX, n), os.path.join(outbox, n)):
            try:
                os.remove(path)
            except OSError:
                pass

    acked = []
    for n in names:
        path = os.path.join(outbox, n)
        try:
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError) as e:
            _log("unreadable, discarding %s: %s" % (n, e))
            acked.append(n)
            continue
        if not post_record(n, record):
            # GandalfAI asleep or down. Stop the pass; the rest stay queued.
            break
        acked.append(n)

    still_queued = [n for n in names if n not in acked]
    sd_del, sd_mir = _sd_reconcile(acked, still_queued)

    # RAM delete AFTER the SD delete, so no overlay whiteout can resurrect a record.
    for n in acked:
        try:
            os.remove(os.path.join(outbox, n))
        except OSError as e:
            _log("RAM delete failed %s: %s" % (n, e))

    if acked or still_queued:
        _log("shipped=%d queued=%d sd_dropped=%d sd_mirrored=%d"
             % (len(acked), len(still_queued), sd_del, sd_mir))
    return len(acked), len(still_queued)


# ── selftest (offline: no network, no SD, no sudo) ────────────────────────────

def _selftest():
    import tempfile
    fails = []
    ran = []

    def check(label, cond):
        ran.append(label)
        print("%-46s %s" % (label, "PASS" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    # ordering
    with tempfile.TemporaryDirectory() as d:
        for n in ("20260721T100000-1-002.json", "20260721T090000-1-001.json"):
            open(os.path.join(d, n), "w").write("{}")
        open(os.path.join(d, "ignore.txt"), "w").write("x")
        q = queued_files(d)
        check("oldest-first ordering", q[0].startswith("20260721T090000"))
        check("non-json ignored", len(q) == 2)

    check("empty outbox is safe", queued_files("/nonexistent/outbox") == [])

    # bound
    names = ["%03d.json" % i for i in range(10)]
    keep, drop = enforce_bound(names, max_queue=4)
    check("bound keeps newest N", keep == names[6:])
    check("bound drops oldest", drop == names[:6])
    check("under cap drops nothing", enforce_bound(names, 99) == (names, []))

    # post_record ack handling
    class R:
        def __init__(self, status, body):
            self.status, self._b = status, body
        def read(self):
            return self._b.encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    check("ack ok:true  -> True",
          post_record("a.json", {}, opener=lambda r, timeout: R(200, '{"ok":true}')))
    check("ack ok:false -> False",
          not post_record("a.json", {}, opener=lambda r, timeout: R(200, '{"ok":false}')))
    check("http 500     -> False",
          not post_record("a.json", {}, opener=lambda r, timeout: R(500, '{}')))
    check("garbage body -> False",
          not post_record("a.json", {}, opener=lambda r, timeout: R(200, 'not json')))

    def boom(r, timeout):
        raise urllib.error.URLError("asleep")
    check("unreachable  -> False (no raise)", not post_record("a.json", {}, opener=boom))

    # mount-state parse: the guard that stops the card being left writable
    ro_line = "/dev/mmcblk0p2 %s ext4 ro,relatime 0 0" % SD_MOUNT
    rw_line = "/dev/mmcblk0p2 %s ext4 rw,relatime 0 0" % SD_MOUNT
    check("mount parse ro", _mounted_mode([rw_line.replace(SD_MOUNT, "/other"), ro_line]) == "ro")
    check("mount parse rw", _mounted_mode([rw_line]) == "rw")
    check("mount parse absent -> None", _mounted_mode(["/dev/x /other ext4 rw 0 0"]) is None)
    check("mount parse ignores short lines", _mounted_mode(["garbage"]) is None)

    # id derived from filename stem
    seen = {}
    hdr = {}
    def cap(r, timeout):
        seen.update(json.loads(r.data.decode()))
        hdr["auth"] = r.get_header("X-iris-auth")
        return R(200, '{"ok":true}')
    post_record("20260721T090000-1-001.json", {"turns": 3}, opener=cap, secret="")
    check("id is filename stem", seen.get("id") == "20260721T090000-1-001")
    check("record passed through", seen.get("record") == {"turns": 3})

    # ── RD-057 auth ──────────────────────────────────────────────────────────
    check("no secret means no auth header", hdr["auth"] is None)
    post_record("a.json", {}, opener=cap, secret="hunter2")
    check("a secret is sent as the auth header", hdr["auth"] == "hunter2")
    check("the secret never leaks into the request body",
          "hunter2" not in json.dumps(seen))
    # THE FAILURE MODE THAT MATTERS: a rejected secret must look like a sleeping
    # GandalfAI, so the record stays queued and is retried. Anything that returns
    # True here would delete a conversation that never arrived.
    check("401 -> False, so the record stays queued and is never dropped",
          not post_record("a.json", {}, opener=lambda r, timeout=None: R(401, '{"ok":false}'),
                          secret="wrong"))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sec.txt")
        open(p, "w").write("  s3cret \n")
        check("secret file is read and stripped", read_secret(p) == "s3cret")
        check("a missing secret file yields no secret, not a crash",
              read_secret(os.path.join(d, "absent.txt")) == "")

    print("\n%d/%d PASS" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        drain()
    except Exception as e:      # never let a drain failure break the log-export cron
        _log("pass failed: %s" % e)
