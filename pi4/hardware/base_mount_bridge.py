import json
import socket
import threading
import time

from core.config import CMD_PORT
from hardware.audio_io import handle_volume_command

_CONFIG_PATH = "/home/pi/iris_config.json"
# WebUI → T4.0 command path. base_mount_bridge owns /dev/ttyIRIS_SERVO, so the
# web process (which cannot touch the port) sends one-line commands here as UDP
# and we write them to the Teensy. Used for the Person Sensor LED toggle (PSLED=).
_SERVO_CMD_PORT     = 10510
_SERVO_CONFIG_PATH  = "/home/pi/servo_config.json"

_DEFAULT_GESTURE_MAP = {
    "VOL+":    "VOL+",
    "VOL-":    "VOL-",
    "STOP":    "STOP",
    "RIGHT":   "STOP",
    "LISTEN":  "LISTEN",
    "FORWARD": "LISTEN",
    "BACKWARD":"WAKE",
    "CW":      "MUTE",
    "CCW":     "SKIP",
}

# Restore level used by MUTE toggle (mutable single-element list for closure capture)
_mute_restore = [70]
_muted = [False]


def _load_gesture_map():
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        # Overlay stored map on defaults so gestures added after the config
        # was saved (e.g. RIGHT) still dispatch their default action.
        merged = dict(_DEFAULT_GESTURE_MAP)
        merged.update(cfg.get("GESTURE_MAP", {}))
        return merged
    except Exception:
        return _DEFAULT_GESTURE_MAP


class BaseMountBridge:
    def __init__(self, config, leds=None):
        self._port = getattr(config, "BASE_MOUNT_PORT", "/dev/ttyIRIS_SERVO")
        self._baud = getattr(config, "BASE_MOUNT_BAUD", 115200)
        self._ser = None
        self._leds = leds
        self._write_lock = threading.Lock()

    def start(self):
        import serial as _serial
        try:
            self._ser = _serial.Serial(self._port, self._baud, timeout=1)
            print(f"[BASE] Teensy 4.0 connected on {self._port}", flush=True)
        except Exception as e:
            print(f"[BASE] WARN: could not open {self._port}: {e}", flush=True)
            return
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._cmd_listener, daemon=True).start()
        self._apply_stored_servo_cfg()

    def send(self, cmd):
        """Write one line to the T4.0 serial (thread-safe vs the read loop)."""
        try:
            if self._ser and self._ser.is_open:
                with self._write_lock:
                    self._ser.write((cmd.strip() + "\n").encode())
                print(f"[BASE] >> {cmd.strip()}", flush=True)
                return True
        except Exception as e:
            print(f"[BASE] send error: {e}", flush=True)
        return False

    def _apply_stored_servo_cfg(self):
        """Re-assert persisted T4.0 settings (e.g. Person Sensor LED) on connect.
        A Teensy reboot reverts to firmware defaults, so we re-push on every open."""
        try:
            with open(_SERVO_CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            return
        if "LED" in cfg:
            self.send(f"PSLED={1 if cfg.get('LED') else 0}")

    def _cmd_listener(self):
        """Receive one-line commands from the web process (UDP) and forward to T4.0."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", _SERVO_CMD_PORT))
        except Exception as e:
            print(f"[BASE] cmd listener bind failed: {e}", flush=True)
            return
        while True:
            try:
                data, _ = sock.recvfrom(256)
                cmd = data.decode("utf-8", errors="ignore").strip()
                if cmd:
                    self.send(cmd)
            except Exception as e:
                print(f"[BASE] cmd listener error: {e}", flush=True)
                time.sleep(1)

    def _dispatch(self, action):
        if action == "VOL+":
            try:
                handle_volume_command("louder")
            except Exception as e:
                print(f"[BASE] VOL+ error: {e}", flush=True)
        elif action == "VOL-":
            try:
                handle_volume_command("quieter")
            except Exception as e:
                print(f"[BASE] VOL- error: {e}", flush=True)
        elif action == "STOP":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(b"STOP", ("127.0.0.1", CMD_PORT))
            except Exception as e:
                print(f"[BASE] STOP error: {e}", flush=True)
        elif action == "LISTEN":
            try:
                open("/tmp/iris_manual_listen", "w").close()
            except Exception as e:
                print(f"[BASE] LISTEN error: {e}", flush=True)
        elif action == "SLEEP":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(b"EYES:SLEEP", ("127.0.0.1", CMD_PORT))
            except Exception as e:
                print(f"[BASE] SLEEP error: {e}", flush=True)
        elif action == "WAKE":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(b"EYES:WAKE", ("127.0.0.1", CMD_PORT))
            except Exception as e:
                print(f"[BASE] WAKE error: {e}", flush=True)
        elif action == "MUTE":
            try:
                from hardware.audio_io import get_volume, set_volume
                if not _muted[0]:
                    v = get_volume()
                    if v > 0:
                        _mute_restore[0] = v
                    set_volume(0, allow_zero=True)
                    _muted[0] = True
                    print("[BASE] MUTE: muted", flush=True)
                else:
                    restore = _mute_restore[0] if _mute_restore[0] > 0 else 70
                    set_volume(restore)
                    _muted[0] = False
                    print(f"[BASE] MUTE: unmuted to {restore}", flush=True)
            except Exception as e:
                print(f"[BASE] MUTE error: {e}", flush=True)
        elif action == "SKIP":
            pass
        else:
            print(f"[BASE] unknown action: {action!r}", flush=True)
            return
        if self._leds is not None:
            try:
                self._leds.show_gesture(action)
            except Exception as e:
                print(f"[BASE] LED error: {e}", flush=True)
        # Audible + TFT-mouth gesture acknowledgment (S144). The bridge owns
        # neither the speaker nor the eyes Teensy, so it asks the assistant
        # CMD listener to play the cue. SLEEP/WAKE already have their own audio
        # (goodnight chime / wake quip); SKIP is a no-op, so neither gets a cue.
        _cue = {"VOL+": "VOL+", "VOL-": "VOL-", "STOP": "STOP",
                "LISTEN": "LISTEN"}.get(action)
        if action == "MUTE":
            _cue = "MUTE" if _muted[0] else "UNMUTE"
        if _cue:
            self._send_gcue(_cue)

    def _send_gcue(self, token):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(f"GCUE:{token}".encode(), ("127.0.0.1", CMD_PORT))
        except Exception as e:
            print(f"[BASE] GCUE error: {e}", flush=True)

    def _read_loop(self):
        _err_logged = False
        while True:
            try:
                if self._ser is None or not self._ser.is_open:
                    import serial as _serial
                    self._ser = _serial.Serial(self._port, self._baud, timeout=1)
                    print(f"[BASE] Reconnected on {self._port}", flush=True)
                    _err_logged = False
                    self._apply_stored_servo_cfg()
                line = self._ser.readline().decode("utf-8", errors="ignore").strip()
                if not line or line.startswith("DIAG:"):
                    continue
                gesture_map = _load_gesture_map()
                action = gesture_map.get(line, "SKIP")
                print(f"[GESTURE] gesture={line} action={action}", flush=True)
                if action != "SKIP":
                    self._dispatch(action)
            except Exception as e:
                if not _err_logged:
                    print(f"[BASE] Serial error: {e} -- reconnecting in 5s", flush=True)
                    _err_logged = True
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                time.sleep(5)
