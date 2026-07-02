#!/usr/bin/env python3
"""
hardware/ogle_bridge.py - OGLE vision node -> eye gaze bridge (RD-033).

Reads the frozen OGLE packet from the OGLE ESP32-S3 node over USB-CDC, runs the
track-decision logic (nearest qualifying face, confidence + is_facing gates,
lost-timeout, EMA smoothing), maps the 0..239 frame center to a normalized
-1..1 eye target, and forwards GAZE:x,y / GAZE:LOST to the Teensy 4.1 eyes.

TRANSPORT: commands are sent as UDP datagrams to the assistant CMD listener
(127.0.0.1:CMD_PORT). That listener is the ONLY legal path to /dev/ttyIRIS_EYES
-- teensy_bridge.py is the single owner of that serial port. This daemon never
opens the eyes port itself. (Same rule that iris_sleep/iris_wake follow.)

Frozen input contract (OGLE firmware):
    OGLE,present,x,y,size,conf,facing,count\n
    x,y  = bbox center, 0..239 frame space
    size = bbox area px ; conf = 0..100 ; present/facing/count = ints
    no face: OGLE,0,0,0,0,0,0,0

RD-031 (no unbounded logging): per-frame GAZE traffic is NOT logged. Only
ACQUIRE/LOST transitions, connect/disconnect, and errors print by default.
Set IRIS_OGLE_DEBUG=1 for per-send debug. (The CMD listener + teensy_bridge
both gate the GAZE: echo behind IRIS_DEBUG_SERIAL for the same reason.)

Run standalone:  python3 hardware/ogle_bridge.py
or via the ogle-bridge.service unit (scripts/ogle-bridge.service).
"""
import os
import socket
import time

import serial

# ── Wiring ────────────────────────────────────────────────────────────────────
OGLE_PORT = os.environ.get("OGLE_PORT", "/dev/ttyIRIS_OGLE")
OGLE_BAUD = int(os.environ.get("OGLE_BAUD", "921600"))
CMD_HOST = "127.0.0.1"
CMD_PORT = int(os.environ.get("IRIS_CMD_PORT", "10500"))  # matches core.config.CMD_PORT

# ── Track-decision gates ──────────────────────────────────────────────────────
CONF_GATE = int(os.environ.get("OGLE_CONF_GATE", "60"))        # 0..100, mirrors the old box_confidence>60
MIN_SIZE = int(os.environ.get("OGLE_MIN_SIZE", "1500"))        # bbox area floor: reject tiny far/false faces
FACING_REQUIRED = os.environ.get("OGLE_FACING_REQUIRED", "1").lower() not in ("0", "false", "no")
LOST_TIMEOUT_S = float(os.environ.get("OGLE_LOST_TIMEOUT_S", "1.0"))

# ── Frame -> normalized gaze mapping ──────────────────────────────────────────
# Mirrors the firmware Person Sensor math (src/main.cpp): nx = -(cx/half - 1).
# Half is 119.5 for a 0..239 frame (was 127.5 for the sensor's 0..255). Axis
# flips + Y bias compensate for the OGLE camera's physical mounting -- TUNE AT
# THE BENCH (a face at image-left must pull the eyes the correct way).
FRAME_HALF = 119.5
FLIP_X = os.environ.get("OGLE_FLIP_X", "1").lower() not in ("0", "false", "no")  # default mirror, like the sensor
FLIP_Y = os.environ.get("OGLE_FLIP_Y", "0").lower() not in ("0", "false", "no")
Y_BIAS = float(os.environ.get("OGLE_Y_BIAS", "-0.10"))  # look slightly up (the sensor path used top + h/3)

# ── Smoothing + rate control ──────────────────────────────────────────────────
EMA_ALPHA = float(os.environ.get("OGLE_EMA_ALPHA", "0.4"))    # 0..1, higher = snappier/less smooth
DEADBAND = float(os.environ.get("OGLE_DEADBAND", "0.03"))     # min normalized move to resend
MAX_HZ = float(os.environ.get("OGLE_MAX_HZ", "15"))
MIN_DT = 1.0 / MAX_HZ if MAX_HZ > 0 else 0.0

DEBUG = os.environ.get("IRIS_OGLE_DEBUG", "").lower() not in ("", "0", "false", "no")


def log(msg):
    print(f"[OGLE] {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        print(f"[OGLE] {msg}", flush=True)


def map_norm(cx, cy):
    nx = cx / FRAME_HALF - 1.0
    ny = cy / FRAME_HALF - 1.0
    if FLIP_X:
        nx = -nx
    if FLIP_Y:
        ny = -ny
    ny += Y_BIAS
    return (max(-1.0, min(1.0, nx)), max(-1.0, min(1.0, ny)))


def parse(line):
    if not line.startswith("OGLE,"):
        return None
    parts = line.split(",")
    if len(parts) != 8:
        return None
    try:
        return {
            "present": int(parts[1]),
            "x": int(parts[2]),
            "y": int(parts[3]),
            "size": int(parts[4]),
            "conf": int(parts[5]),
            "facing": int(parts[6]),
            "count": int(parts[7]),
        }
    except ValueError:
        return None


def qualifies(p):
    return (
        p["present"] == 1
        and p["conf"] >= CONF_GATE
        and p["size"] >= MIN_SIZE
        and (not FACING_REQUIRED or p["facing"] == 1)
    )


class GazeSender:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, cmd):
        try:
            self._sock.sendto(cmd.encode(), (CMD_HOST, CMD_PORT))
        except OSError as e:
            log(f"UDP send failed: {e}")


def run():
    sender = GazeSender()
    ser = None
    tracking = False               # currently commanding GAZE (vs LOST)?
    sx = sy = 0.0                   # EMA state
    last_sx = last_sy = None        # last value actually sent (deadband ref)
    last_send_t = 0.0
    last_qualify_t = 0.0
    log(
        f"start: port={OGLE_PORT} baud={OGLE_BAUD} -> CMD {CMD_HOST}:{CMD_PORT} "
        f"(conf>={CONF_GATE} size>={MIN_SIZE} facing_req={FACING_REQUIRED} "
        f"lost={LOST_TIMEOUT_S}s flipX={FLIP_X} flipY={FLIP_Y})"
    )
    while True:
        if ser is None:
            try:
                ser = serial.Serial(OGLE_PORT, OGLE_BAUD, timeout=1)
                ser.reset_input_buffer()
                log(f"connected on {OGLE_PORT}")
            except (serial.SerialException, OSError) as e:
                log(f"cannot open {OGLE_PORT}: {e} -- retry in 3s")
                time.sleep(3)
                continue

        try:
            raw = ser.readline().decode(errors="ignore").strip()
        except (serial.SerialException, OSError) as e:
            log(f"serial disconnected: {e} -- reconnecting")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            if tracking:  # don't strand the eyes on a frozen target
                sender.send("GAZE:LOST")
                tracking = False
                log("LOST (serial drop)")
            time.sleep(2)
            continue

        now = time.monotonic()
        p = parse(raw) if raw else None

        if p and qualifies(p):
            last_qualify_t = now
            nx, ny = map_norm(p["x"], p["y"])
            if not tracking:
                sx, sy = nx, ny          # seed EMA on acquire -> no swing from stale state
                tracking = True
                last_sx = last_sy = None
                log(f"ACQUIRE conf={p['conf']} size={p['size']}")
            else:
                sx = EMA_ALPHA * nx + (1.0 - EMA_ALPHA) * sx
                sy = EMA_ALPHA * ny + (1.0 - EMA_ALPHA) * sy

            moved = (
                last_sx is None
                or abs(sx - last_sx) > DEADBAND
                or abs(sy - last_sy) > DEADBAND
            )
            if moved and (now - last_send_t) >= MIN_DT:
                sender.send(f"GAZE:{sx:.3f},{sy:.3f}")
                last_sx, last_sy = sx, sy
                last_send_t = now
                dbg(f"GAZE {sx:.3f},{sy:.3f} (raw {p['x']},{p['y']} conf={p['conf']})")
        else:
            if tracking and (now - last_qualify_t) >= LOST_TIMEOUT_S:
                sender.send("GAZE:LOST")
                tracking = False
                log("LOST")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
