"""
hardware/teensy_bridge.py - Teensy 4.1 serial bridge (eyes + mouth TFT)
SINGLE OWNER of /dev/ttyIRIS_EYES. No other module may open this port.
All other callers (iris_web.py, cron scripts) must use UDP → 127.0.0.1:CMD_PORT.

Provides:
  TeensyBridge(port, baud)
    .send_emotion(emotion)      — sends EMOTION:<X>\n
    .send_command(cmd)          — sends <cmd>\n (raw, no prefix)
    .close()                    — stops reader thread, closes serial
"""

import os
import threading
import time

import serial

from core.config import TEENSY_PORT, TEENSY_BAUD

# RD-031: routine high-rate serial traffic was ~90% of journal volume — inbound
# sleep-frame echoes ([SR] …) plus per-turn outbound MOUTH/MOUTH_INTENSITY updates
# (2 Hz during TTS + idle breathe). Suppress those echoes by default. Set
# IRIS_DEBUG_SERIAL=1 to log every serial line again. Errors, DROPs,
# connect/disconnect, [VER] and FACE: state are always logged.
DEBUG_SERIAL = os.environ.get("IRIS_DEBUG_SERIAL", "").lower() not in ("", "0", "false", "no")

# Outbound command prefixes that fire at high rate (mouth animation / idle breathe;
# GAZE: added RD-033 — OGLE streams gaze targets at the camera frame rate).
_ROUTINE_TX_PREFIXES = ("MOUTH:", "MOUTH_INTENSITY:", "GAZE:")
# Inbound line prefixes that are routine per-frame chatter.
_ROUTINE_RX_PREFIXES = ("[SR]",)

# S185 (Vision Cal hardening): FACE:1/FACE:0 and the boot-time "Person Sensor
# detected"/"no ACK" lines are edge-triggered -- printed once at Teensy boot or
# on a lock/lose transition, never as a "still here" signal. assistant.service
# restarts routinely (every deploy, per project rules) and the Teensy keeps
# running through that restart, so a steady, continuously-tracking sensor can
# go all day without asserting anything new to the journal -- iris_web.py's
# /api/ps/status then reads that silence as UNKNOWN even though the sensor and
# link are fine. This periodic, bounded (RD-031) heartbeat gives it fresh
# evidence to key off instead.
PS_HEARTBEAT_INTERVAL_S = 60


class TeensyBridge:
    def __init__(self, port: str = TEENSY_PORT, baud: int = TEENSY_BAUD, on_reconnect=None):
        self._port = port
        self._baud = baud
        self._ser = None
        self._lock = threading.Lock()
        self._active = True
        self._on_reconnect = on_reconnect
        self._ps_present = None   # None=unknown, True/False once a real signal is seen
        self._ps_face    = None
        self._ps_last_hb = 0.0
        threading.Thread(target=self._reader, daemon=True).start()

    def _open(self):
        try:
            s = serial.Serial(self._port, self._baud, timeout=1)
            s.reset_input_buffer()
            time.sleep(0.1)        # let Teensy CDC handler settle after open
            s.write(b"VERSION\n")  # request firmware version; response logged by _reader
            print(f"[EYES] Teensy connected on {self._port}", flush=True)
            return s
        except (serial.SerialException, OSError) as e:
            print(f"[EYES] Cannot open {self._port}: {e} -- will retry", flush=True)
            return None

    def _reader(self):
        _opened_once = False
        while self._active:
            # Snapshot the handle under the lock: a concurrent failed send sets
            # self._ser = None, and reading the attribute unlocked mid-loop used
            # to raise an uncaught AttributeError that silently killed this
            # thread -- no reconnect ever, all sends DROP until service restart.
            reconnected = False
            with self._lock:
                if self._ser is None or not self._ser.is_open:
                    self._ser = self._open()
                    reconnected = self._ser is not None
                ser = self._ser
            # Fire on_reconnect only on re-opens (not the initial open at startup,
            # which is covered by the explicit _push_ps_config call in main()).
            if reconnected and _opened_once and self._on_reconnect:
                self._on_reconnect()
            if reconnected:
                _opened_once = True
            if ser is None:
                time.sleep(5)
                continue
            try:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    if "Person Sensor detected" in line:
                        self._ps_present = True
                    elif "no ACK at 0x62" in line or "No Person Sensor" in line:
                        self._ps_present = False
                    elif "FACE:1" in line:
                        self._ps_face = True
                    elif "FACE:0" in line:
                        self._ps_face = False
                    if DEBUG_SERIAL or not line.startswith(_ROUTINE_RX_PREFIXES):
                        print(f"[EYES] << {line}", flush=True)
                now = time.time()
                if self._ps_present is not None and now - self._ps_last_hb >= PS_HEARTBEAT_INTERVAL_S:
                    self._ps_last_hb = now
                    face_s = "?" if self._ps_face is None else int(self._ps_face)
                    print(f"[EYES] PS_HEARTBEAT present={int(self._ps_present)} face={face_s}",
                          flush=True)
            except (serial.SerialException, OSError):
                print("[EYES] Serial disconnected -- will retry", flush=True)
                with self._lock:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    if self._ser is ser:
                        self._ser = None
                time.sleep(5)
            except Exception as e:
                # Belt-and-braces: the reader thread must never die. Anything
                # unexpected (e.g. races on a handle being torn down by a send)
                # logs and retries instead of silently ending reconnects.
                print(f"[EYES] Reader error: {e} -- will retry", flush=True)
                time.sleep(5)

    def send_emotion(self, emotion: str) -> bool:
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                print(f"[EYES] DROP EMOTION:{emotion} -- port not open", flush=True)
                return False
            try:
                self._ser.write(f"EMOTION:{emotion}\n".encode())
                self._ser.flush()
                print(f"[EYES] >> EMOTION:{emotion}", flush=True)
                return True
            except (serial.SerialException, OSError) as e:
                print(f"[EYES] Send failed: {e}", flush=True)
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                return False

    def send_command(self, cmd: str) -> bool:
        """Send a raw command string (no EMOTION: prefix) to the Teensy."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                print(f"[EYES] DROP {cmd} -- port not open", flush=True)
                return False
            try:
                self._ser.write(f"{cmd}\n".encode())
                self._ser.flush()
                if DEBUG_SERIAL or not cmd.startswith(_ROUTINE_TX_PREFIXES):
                    print(f"[EYES] >> {cmd}", flush=True)
                return True
            except (serial.SerialException, OSError) as e:
                print(f"[EYES] Send failed: {e}", flush=True)
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                return False

    def close(self):
        self._active = False
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
