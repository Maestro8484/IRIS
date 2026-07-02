#!/usr/bin/env python3
"""
iris_post.py  -  IRIS Power-On Self-Test v1.0
5-layer diagnostic: hardware → services → display → pipeline → config.
Trigger: assistant.py startup, /api/post web endpoint, or direct SSH.
Log: /home/pi/logs/iris_post.log
LEDs: layer-colored progress (cyan/purple/amber/orange/red), green flash on PASS,
      red 3× flash + freeze on FAIL.
"""

import datetime, json, os, socket, subprocess, sys, time, threading
sys.path.insert(0, "/home/pi")

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
LOG_PATH    = "/home/pi/logs/iris_post.log"
CONFIG_PATH = "/home/pi/iris_config.json"
INTENT_LOG  = "/home/pi/logs/iris_intent.log"

# APA102 layer colors (R, G, B)
_LED_LAYERS = [
    (0,   180, 220),   # L0  cyan
    (140,   0, 220),   # L1  purple
    (255, 160,   0),   # L2  amber
    (255,  80,   0),   # L3  orange
    (220,   0,   0),   # L4  red
]
_LED_PASS = (0, 220,  80)   # green
_LED_FAIL = (220,  0,   0)  # red

# ── Config defaults (overridden by core.config if importable) ─────────────────
TEENSY_PORT  = "/dev/ttyIRIS_EYES"
TEENSY_BAUD  = 115200
CMD_PORT     = 10500
GANDALF      = "192.168.1.3"
OLLAMA_PORT  = 11434
KOKORO_PORT  = 8004
WHISPER_PORT = 10300
PIPER_PORT   = 10200
OWW_PORT     = 10400
WOL_BOOT_TIMEOUT  = 120
WOL_POLL_INTERVAL = 5
GANDALF_MAC  = "A4:BB:6D:CA:83:20"
OLLAMA_MODEL_ADULT  = "iris"
OLLAMA_MODEL_KIDS   = "iris-kids"
NUM_LEDS            = 3
DEFAULT_EYE_IDX     = 0
MOUTH_INTENSITY_IDLE = 8   # post-boot resting brightness; matches core.config (was 3 ≈2.7% near-black, S130)

try:
    from core.config import (
        TEENSY_PORT, TEENSY_BAUD, CMD_PORT, GANDALF, OLLAMA_PORT,
        WHISPER_PORT, PIPER_PORT, OWW_PORT,
        WOL_BOOT_TIMEOUT, WOL_POLL_INTERVAL, GANDALF_MAC,
        OLLAMA_MODEL_ADULT, OLLAMA_MODEL_KIDS, NUM_LEDS,
        DEFAULT_EYE_IDX, MOUTH_INTENSITY_IDLE,
    )
    try:
        from core.config import KOKORO_BASE_URL
        KOKORO_PORT = int(KOKORO_BASE_URL.rstrip("/").rsplit(":", 1)[-1])
    except Exception:
        pass
except Exception as _ce:
    print(f"[POST] core.config import failed ({_ce}) -- using built-in defaults", flush=True)


# ── Internal POST state (reset per run_post() call) ───────────────────────────

class _POST:
    """Encapsulates one POST run to avoid module-level mutable state."""

    def __init__(self, leds, teensy, pa, verbose):
        self.leds    = leds
        self.teensy  = teensy
        self.pa      = pa
        self.verbose = verbose
        self.results = []
        self.first_fail_layer = None

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, line):
        msg = f"{datetime.datetime.now().isoformat(timespec='seconds')} {line}"
        if self.verbose:
            print(msg, flush=True)
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def record(self, layer, check, status, detail=""):
        label = f"[{layer}] {check}"
        dots  = max(1, 48 - len(label))
        suffix = f" ({detail})" if detail else ""
        self.log(f"[{layer}] {check} {'.' * dots} {status}{suffix}")
        self.results.append({"layer": layer, "check": check,
                              "status": status, "detail": detail})
        if status == FAIL and self.first_fail_layer is None:
            self.first_fail_layer = layer
        return status

    # ── LED helpers ───────────────────────────────────────────────────────────

    def led(self, color):
        if self.leds is None:
            return
        try:
            self.leds._write([color] * self.leds.n)
        except Exception:
            pass

    def led_flash(self, color, n=3, on_ms=200, off_ms=200):
        for _ in range(n):
            self.led(color);      time.sleep(on_ms  / 1000)
            self.led((0, 0, 0));  time.sleep(off_ms / 1000)

    # ── Transport helpers ─────────────────────────────────────────────────────

    def udp(self, cmd):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto((cmd.strip() + "\n").encode(), ("127.0.0.1", CMD_PORT))
            return True
        except Exception:
            return False

    def send_display(self, cmd, dwell=0.0):
        """Send Teensy display command via UDP (or direct if teensy available)."""
        if self.teensy is not None:
            try:
                self.teensy.send_command(cmd)
            except Exception:
                self.udp(cmd)
        else:
            self.udp(cmd)
        if dwell:
            time.sleep(dwell)

    @staticmethod
    def tcp_check(host, port, retries=3, delay=5, timeout=3):
        for i in range(retries):
            try:
                socket.create_connection((host, port), timeout=timeout).close()
                return True
            except (OSError, ConnectionRefusedError):
                if i < retries - 1:
                    time.sleep(delay)
        return False

    @staticmethod
    def wol(mac, ip="255.255.255.255", port=9):
        mac_b = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        pkt   = b"\xff" * 6 + mac_b * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, (ip, port))

    # ── L0 — Hardware presence ────────────────────────────────────────────────

    def l0_serial(self):
        try:
            if self.teensy is not None:
                self.teensy.send_command("EMOTION:NEUTRAL")
                return self.record("L0", f"serial {TEENSY_PORT}", PASS)
            import serial
            ser = serial.Serial(TEENSY_PORT, TEENSY_BAUD, timeout=2)
            ser.write(b"EMOTION:NEUTRAL\n")
            ser.close()
            return self.record("L0", f"serial {TEENSY_PORT}", PASS)
        except Exception as e:
            return self.record("L0", f"serial {TEENSY_PORT}", FAIL, str(e)[:60])

    def l0_mic(self):
        try:
            import pyaudio
            from hardware.audio_io import _find_mic_device_index
            _pa = pyaudio.PyAudio()
            idx = _find_mic_device_index()
            mic = _pa.open(rate=16000, channels=2, format=pyaudio.paInt16,
                           input=True, frames_per_buffer=1024,
                           input_device_index=idx)
            mic.stop_stream(); mic.close(); _pa.terminate()
            return self.record("L0", "mic wm8960 open", PASS)
        except Exception as e:
            return self.record("L0", "mic wm8960 open", FAIL, str(e)[:60])

    def l0_camera(self):
        # Camera is optional — failure never blocks startup
        tmp = "/tmp/iris_post_cam.jpg"
        try:
            r = subprocess.run(
                ["rpicam-still", "-o", tmp, "--nopreview", "-t", "500",
                 "--width", "640", "--height", "480"],
                capture_output=True, timeout=12)
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.unlink(tmp)
                return self.record("L0", "camera capture", PASS)
            if os.path.exists(tmp):
                os.unlink(tmp)
            return self.record("L0", "camera capture", WARN,
                               r.stderr.decode(errors="ignore")[:60] or "no file written")
        except Exception as e:
            return self.record("L0", "camera capture", WARN, str(e)[:60])

    def l0_gesture(self):
        # PAJ7620U2 sits on Teensy 4.0 I2C (pins 18/19), not on Pi4 I2C bus 1.
        # Pi4 smbus can never reach it directly. Check is always WARN; sensor
        # health is confirmed via Teensy serial DIAG at boot, not here.
        try:
            import smbus
            bus = smbus.SMBus(1)
            bus.read_byte(0x73)
            bus.close()
            return self.record("L0", "gesture sensor I2C 0x73", PASS)
        except Exception:
            return self.record("L0", "gesture sensor I2C 0x73", WARN,
                               "Teensy-side I2C -- verify via serial DIAG")

    # ── L1 — Network + services ───────────────────────────────────────────────

    def l1_gandalf(self):
        if self.tcp_check(GANDALF, OLLAMA_PORT, retries=1, timeout=3):
            return self.record("L1", f"GandalfAI :{OLLAMA_PORT}", PASS)
        self.log(f"[L1] GandalfAI unreachable -- firing WoL {GANDALF_MAC}")
        try:
            self.wol(GANDALF_MAC, GANDALF)
        except Exception as e:
            self.log(f"[L1] WoL send failed: {e}")
        try:
            from hardware.audio_io import play_wol_beep
            if self.pa is not None:
                play_wol_beep(self.pa)
        except Exception:
            pass
        deadline = time.monotonic() + WOL_BOOT_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(WOL_POLL_INTERVAL)
            if self.tcp_check(GANDALF, OLLAMA_PORT, retries=1, timeout=3):
                boot_s = int(WOL_BOOT_TIMEOUT - (deadline - time.monotonic()))
                return self.record("L1", f"GandalfAI :{OLLAMA_PORT}", PASS, f"boot {boot_s}s")
            self.log(f"[L1] WoL: waiting ({int(deadline - time.monotonic())}s remaining)")
        return self.record("L1", f"GandalfAI :{OLLAMA_PORT}", FAIL, "WoL timeout")

    def l1_services(self):
        specs = [
            (GANDALF,      KOKORO_PORT,   "Kokoro",        WARN),   # non-blocking: Piper is fallback
            (GANDALF,      WHISPER_PORT,  "Whisper",       FAIL),
            (GANDALF,      PIPER_PORT,    "Piper",         WARN),
            ("127.0.0.1",  OWW_PORT,      "OpenWakeWord",  FAIL),
        ]
        kokoro_ok = True
        piper_ok  = True
        for host, port, name, on_fail in specs:
            ok = self.tcp_check(host, port)
            self.record("L1", f"{name} :{port}", PASS if ok else on_fail)
            if name == "Kokoro":
                kokoro_ok = ok
            elif name == "Piper":
                piper_ok = ok
        if not kokoro_ok:
            fallback = "Piper fallback active" if piper_ok else "Piper also down -- no TTS fallback"
            self.log(f"[L1] TTS: Kokoro down -- {fallback}")

    def l1_models(self):
        try:
            import requests
            r = requests.get(f"http://{GANDALF}:{OLLAMA_PORT}/api/tags", timeout=10)
            r.raise_for_status()
            names = {m["name"].split(":")[0] for m in r.json().get("models", [])}
            missing = [m for m in (OLLAMA_MODEL_ADULT, OLLAMA_MODEL_KIDS) if m not in names]
            if not missing:
                return self.record("L1",
                    f"Ollama models {OLLAMA_MODEL_ADULT}+{OLLAMA_MODEL_KIDS}", PASS)
            return self.record("L1",
                f"Ollama models {OLLAMA_MODEL_ADULT}+{OLLAMA_MODEL_KIDS}", FAIL,
                f"missing: {','.join(missing)}")
        except Exception as e:
            return self.record("L1", "Ollama models", FAIL, str(e)[:60])

    # ── L2 — Teensy display exercise ──────────────────────────────────────────

    def l2_display(self):
        try:
            for i in range(9):
                self.send_display(f"MOUTH:{i}", 0.4)
            self.record("L2", "MOUTH:0-8 cycle", PASS)
        except Exception as e:
            self.record("L2", "MOUTH:0-8 cycle", FAIL, str(e)[:60])

        try:
            for emo in ("NEUTRAL", "HAPPY", "ANGRY", "CONFUSED", "NEUTRAL"):
                self.send_display(f"EMOTION:{emo}", 0.4)
            self.record("L2", "EMOTION sweep", PASS)
        except Exception as e:
            self.record("L2", "EMOTION sweep", FAIL, str(e)[:60])

        try:
            self.send_display("EYES:SLEEP", 2.0)
            self.send_display("EYES:WAKE",  0.5)
            self.record("L2", "EYES:SLEEP/WAKE", PASS)
        except Exception as e:
            self.record("L2", "EYES:SLEEP/WAKE", FAIL, str(e)[:60])

        try:
            for idx in (0, 3, 6):
                self.send_display(f"EYE:{idx}", 0.4)
            self.record("L2", "EYE index cycle", PASS)
        except Exception as e:
            self.record("L2", "EYE index cycle", FAIL, str(e)[:60])

        try:
            for v in (1, 8, 15):
                self.send_display(f"MOUTH_INTENSITY:{v}", 0.3)
            self.record("L2", "MOUTH_INTENSITY ramp", PASS)
        except Exception as e:
            self.record("L2", "MOUTH_INTENSITY ramp", FAIL, str(e)[:60])

        # Restore display to defaults after exercise — without this the Teensy
        # would be left on EYE:6 (bigBlue) and MOUTH_INTENSITY:15 after every boot.
        self.send_display(f"EYE:{DEFAULT_EYE_IDX}", 0.1)
        self.send_display(f"MOUTH_INTENSITY:{MOUTH_INTENSITY_IDLE}", 0.1)

    def l2_firmware_version(self):
        """Read firmware version from the most recent [VER] line in the assistant journal."""
        try:
            result = subprocess.run(
                ["journalctl", "-u", "assistant", "--no-pager", "-n", "500",
                 "--output=short-iso"],
                capture_output=True, text=True, timeout=10)
            ver_str = None
            for line in reversed(result.stdout.splitlines()):
                if "[VER] IRIS-EYES" in line:
                    ver_str = line.split("[VER] IRIS-EYES")[-1].strip()
                    break
            if ver_str:
                return self.record("L2", "firmware version", PASS, ver_str)
            return self.record("L2", "firmware version", WARN,
                               "no [VER] in journal — flash versioned firmware (S87b+)")
        except Exception as e:
            return self.record("L2", "firmware version", WARN, str(e)[:60])

    # ── L3 — Pipeline smoke ───────────────────────────────────────────────────

    def l3_router(self):
        try:
            from core.intent_router import IntentRouter, ROUTE_UTILITY
            from state.state_manager import state as _st
            router = IntentRouter()
            result = router.classify("what time is it", _st)
            if result.route == ROUTE_UTILITY:
                return self.record("L3", "intent router UTILITY", PASS)
            return self.record("L3", "intent router UTILITY", FAIL,
                               f"got route={result.route}")
        except Exception as e:
            return self.record("L3", "intent router UTILITY", FAIL, str(e)[:60])

    def l3_tts(self):
        try:
            import requests
            r = requests.post(
                f"http://{GANDALF}:{KOKORO_PORT}/v1/audio/speech",
                json={"model": "kokoro", "input": "test",
                      "voice": "bm_lewis", "response_format": "wav"},
                timeout=15)
            r.raise_for_status()
            if len(r.content) > 100:
                return self.record("L3", "TTS round-trip Kokoro", PASS)
            return self.record("L3", "TTS round-trip Kokoro", FAIL, "empty PCM response")
        except Exception as e:
            return self.record("L3", "TTS round-trip Kokoro", FAIL, str(e)[:60])

    def l3_llm(self):
        try:
            import requests
            r = requests.post(
                f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
                json={"model": OLLAMA_MODEL_ADULT, "prompt": "hello", "stream": False},
                timeout=30)
            r.raise_for_status()
            if r.json().get("response"):
                return self.record("L3", "LLM smoke Ollama", PASS)
            return self.record("L3", "LLM smoke Ollama", FAIL, "no response key")
        except Exception as e:
            return self.record("L3", "LLM smoke Ollama", FAIL, str(e)[:60])

    def l3_intent_log(self):
        try:
            os.makedirs(os.path.dirname(INTENT_LOG), exist_ok=True)
            with open(INTENT_LOG, "a") as f:
                f.write("")
            return self.record("L3", "intent log writable", PASS)
        except Exception as e:
            return self.record("L3", "intent log writable", FAIL, str(e)[:60])

    # ── L4 — Config + persistence ─────────────────────────────────────────────

    def l4_config(self):
        try:
            from core.config import _OVERRIDABLE
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            unknown = [k for k in cfg if k not in _OVERRIDABLE]
            if not unknown:
                return self.record("L4", "iris_config.json parse", PASS)
            return self.record("L4", "iris_config.json parse", WARN,
                               f"unknown keys: {', '.join(unknown[:3])}")
        except FileNotFoundError:
            return self.record("L4", "iris_config.json parse", WARN,
                               "file not found (defaults active)")
        except Exception as e:
            return self.record("L4", "iris_config.json parse", WARN,
                               "parse error (defaults active): " + str(e)[:40])

    def l4_md5(self):
        src = "/home/pi/assistant.py"
        dst = "/media/root-ro/home/pi/assistant.py"
        try:
            r = subprocess.run(
                ["bash", "-c",
                 f"md5sum {src} {dst} 2>/dev/null | awk '{{print $1}}' | sort -u | wc -l"],
                capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "1":
                return self.record("L4", "RAM vs SD md5", PASS)
            return self.record("L4", "RAM vs SD md5", WARN,
                               "RAM/SD mismatch -- persist needed")
        except Exception as e:
            return self.record("L4", "RAM vs SD md5", FAIL, str(e)[:60])

    def l4_ownership(self):
        try:
            import pwd
            pi_uid = pwd.getpwnam("pi").pw_uid
            st = os.stat(CONFIG_PATH)
            if st.st_uid == pi_uid:
                return self.record("L4", "config owner pi:pi", PASS)
            return self.record("L4", "config owner pi:pi", FAIL,
                               f"uid={st.st_uid} expected {pi_uid}")
        except FileNotFoundError:
            return self.record("L4", "config owner pi:pi", WARN,
                               "iris_config.json not found")
        except Exception as e:
            return self.record("L4", "config owner pi:pi", FAIL, str(e)[:60])

    # ── Main sequence ─────────────────────────────────────────────────────────

    def run(self):
        self.log("[POST] IRIS Power-On Self-Test v1.0")

        self.log("[LED] POST indicator: cyan (L0)");   self.led(_LED_LAYERS[0])
        self.l0_serial()
        self.l0_mic()
        self.l0_camera()
        self.l0_gesture()

        self.log("[LED] POST indicator: purple (L1)"); self.led(_LED_LAYERS[1])
        self.l1_gandalf()
        self.l1_services()
        self.l1_models()

        self.log("[LED] POST indicator: amber (L2)");  self.led(_LED_LAYERS[2])
        self.l2_display()
        self.l2_firmware_version()

        self.log("[LED] POST indicator: orange (L3)"); self.led(_LED_LAYERS[3])
        self.l3_router()
        self.l3_tts()
        self.l3_llm()
        self.l3_intent_log()

        self.log("[LED] POST indicator: red (L4)");    self.led(_LED_LAYERS[4])
        self.l4_config()
        self.l4_md5()
        self.l4_ownership()

        # ── L5: Verdict ───────────────────────────────────────────────────────
        # Only serial (eyes/mouth) and mic failures block startup.
        # All other FAILs are demoted to informational — IRIS boots degraded
        # rather than entering a systemd restart loop.
        _BLOCKING = {f"serial {TEENSY_PORT}", "mic wm8960 open"}
        n_total = len(self.results)
        n_pass  = sum(1 for r in self.results if r["status"] == PASS)
        n_warn  = sum(1 for r in self.results if r["status"] == WARN)
        n_fail  = sum(1 for r in self.results if r["status"] == FAIL)
        hard_fails = [r for r in self.results
                      if r["status"] == FAIL and r["check"] in _BLOCKING]
        verdict = "AUTHORIZED" if not hard_fails else "FAIL"

        self.log("─" * 57)
        self.log(f"[POST] RESULT: {n_pass}/{n_total} PASS  WARN: {n_warn}  FAIL: {n_fail}")
        if verdict == "AUTHORIZED":
            self.log("[POST] assistant startup AUTHORIZED")
            self.log("[LED] POST complete: green flash → idle")
            self.led_flash(_LED_PASS, n=3)
            self.led((0, 60, 80))   # dim cyan idle
        else:
            self.log(f"[POST] assistant startup BLOCKED (first FAIL in {self.first_fail_layer})")
            self.log(f"[LED] FAIL in {self.first_fail_layer} -- red flash 3x, freeze")
            self.led_flash(_LED_FAIL, n=3)
            self.led(_LED_FAIL)
            if self.pa is not None:
                try:
                    from services.tts import synthesize
                    from hardware.audio_io import play_pcm
                    pcm = synthesize("IRIS self-test failed. Check the web panel.")
                    play_pcm(pcm, self.pa)
                except Exception as _te:
                    self.log(f"[POST] TTS alert failed: {_te}")

        return {
            "verdict":  verdict,
            "n_pass":   n_pass,
            "n_warn":   n_warn,
            "n_fail":   n_fail,
            "n_total":  n_total,
            "checks":   self.results,
            "ts":       datetime.datetime.now().isoformat(timespec="seconds"),
        }


# ── Public API ────────────────────────────────────────────────────────────────

def run_post(leds=None, teensy=None, pa=None, verbose=True) -> dict:
    """Run IRIS POST sequence. Returns result dict."""
    return _POST(leds, teensy, pa, verbose).run()


# ── Standalone (SSH) entry point ──────────────────────────────────────────────

if __name__ == "__main__":
    _leds = None
    _pa   = None
    try:
        from hardware.led import APA102
        _leds = APA102(NUM_LEDS)
    except Exception as e:
        print(f"[POST] APA102 init failed (no LED indicator): {e}", flush=True)
    try:
        import pyaudio
        _pa = pyaudio.PyAudio()
    except Exception:
        pass

    result = run_post(leds=_leds, pa=_pa, verbose=True)

    if _pa:
        _pa.terminate()
    if _leds:
        try:
            _leds.show_idle()
        except Exception:
            pass

    sys.exit(0 if result["verdict"] == "AUTHORIZED" else 1)
