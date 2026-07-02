"""
hardware/led.py - APA102 LED driver
3x APA102 RGB LEDs via SPI bus 0, CE1.
All animation methods are non-blocking (run in daemon threads).
Call stop_anim() or any show_* to cancel the running animation.
"""

import math
import threading
import time

import spidev

_SLEEP_CFG_PATH = "/home/pi/iris_config.json"
_SLEEP_DEFAULTS = {"LED_SLEEP_PEAK": 8, "LED_SLEEP_FLOOR": 1, "LED_SLEEP_PERIOD": 8.0, "LED_SLEEP_BRIGHT": 0xE3}


class APA102:
    def __init__(self, n: int = 3):
        self.n = n
        self.spi = spidev.SpiDev()
        self.spi.open(0, 1)
        self.spi.max_speed_hz = 1_000_000
        self._pixels = [(0, 0, 0)] * n
        self._lock = threading.Lock()
        self._anim_thread = None
        self._stop_anim = threading.Event()

    # ── Low-level write ───────────────────────────────────────────────────────

    def _write(self, pixels, brightness=0xFF):
        buf = [0x00] * 4
        for r, g, b in pixels:
            buf += [brightness, b, g, r]
        buf += [0xFF] * 4
        with self._lock:
            self.spi.xfer2(buf)

    # ── Single-frame helpers ──────────────────────────────────────────────────

    def set_all(self, r: int, g: int, b: int):
        self._write([(r, g, b)] * self.n)

    def set_pixel(self, i: int, r: int, g: int, b: int):
        px = list(self._pixels)
        px[i] = (r, g, b)
        self._pixels = px
        self._write(px)

    def off(self):
        self._write([(0, 0, 0)] * self.n)

    # ── Animation control ─────────────────────────────────────────────────────

    def stop_anim(self):
        self._stop_anim.set()
        if self._anim_thread and self._anim_thread.is_alive():
            self._anim_thread.join(timeout=2)
        self._stop_anim.clear()

    def _run_anim(self, fn):
        self.stop_anim()
        self._anim_thread = threading.Thread(target=fn, daemon=True)
        self._anim_thread.start()

    # ── Status animations ─────────────────────────────────────────────────────

    def show_idle(self):
        def anim():
            from core.config import LED_IDLE_PEAK, LED_IDLE_FLOOR, LED_IDLE_PERIOD
            steps = 80; period = LED_IDLE_PERIOD; floor = LED_IDLE_FLOOR; peak = LED_IDLE_PEAK
            while not self._stop_anim.is_set():
                for i in range(steps):
                    if self._stop_anim.is_set():
                        return
                    t = i / steps
                    s = (math.sin(2 * math.pi * t - math.pi / 2) + 1) / 2
                    v = int(floor + (peak - floor) * (s ** 1.8))
                    self._write([(0, v, v)] * self.n)
                    time.sleep(period / steps)
        self._run_anim(anim)

    def show_idle_kids(self):
        def anim():
            from core.config import LED_KIDS_PEAK, LED_KIDS_PERIOD
            steps = 80; period = LED_KIDS_PERIOD; floor = 1; peak = LED_KIDS_PEAK
            while not self._stop_anim.is_set():
                for i in range(steps):
                    if self._stop_anim.is_set():
                        return
                    t = i / steps
                    s = (math.sin(2 * math.pi * t - math.pi / 2) + 1) / 2
                    v = int(floor + (peak - floor) * (s ** 1.8))
                    self._write([(v, v, 0)] * self.n)
                    time.sleep(period / steps)
        self._run_anim(anim)

    def show_kids_mode_on(self):
        def anim():
            for _ in range(3):
                if self._stop_anim.is_set():
                    return
                self._write([(10, 10, 0)] * self.n); time.sleep(0.15)
                self._write([(0, 0, 0)] * self.n);     time.sleep(0.10)
        self._run_anim(anim)

    def show_kids_mode_off(self):
        def anim():
            for _ in range(3):
                if self._stop_anim.is_set():
                    return
                self._write([(0, 10, 10)] * self.n); time.sleep(0.15)
                self._write([(0, 0, 0)] * self.n);     time.sleep(0.10)
        self._run_anim(anim)

    def show_wake(self):
        self.stop_anim(); self.set_all(8, 8, 8)

    def show_recording(self):
        self.stop_anim(); self.set_all(12, 0, 0)

    def show_thinking(self):
        def anim():
            i = 0
            while not self._stop_anim.is_set():
                px = [(0, 0, 0)] * self.n
                px[i % self.n] = (0, 0, 10)
                self._write(px); time.sleep(0.12); i += 1
        self._run_anim(anim)

    def show_speaking(self):
        self.stop_anim(); self.set_all(0, 8, 0)

    def show_error(self):
        def anim():
            for _ in range(6):
                if self._stop_anim.is_set():
                    return
                self._write([(12, 0, 0)] * self.n); time.sleep(0.1)
                self._write([(0, 0, 0)] * self.n);  time.sleep(0.1)
        self._run_anim(anim)

    def show_followup(self):
        def anim():
            while not self._stop_anim.is_set():
                for v in list(range(0, 9, 3)) + list(range(9, 0, -3)):
                    if self._stop_anim.is_set():
                        return
                    self._write([(v, 0, v)] * self.n); time.sleep(0.04)
        self._run_anim(anim)

    def show_ptt(self):
        self.stop_anim(); self.set_all(8, 6, 0)

    def show_wol(self):
        """Orange breathe for Wake-on-LAN wait. Uses _run_anim() so gestures can preempt it cleanly."""
        def anim():
            while not self._stop_anim.is_set():
                for v in list(range(0, 70, 4)) + list(range(70, 0, -4)):
                    if self._stop_anim.is_set():
                        return
                    self._write([(v, v // 3, 0)] * self.n)
                    time.sleep(0.05)
        self._run_anim(anim)

    # ── Gesture feedback ─────────────────────────────────────────────────────

    def show_gesture(self, action: str):
        """Transient gesture feedback. Non-blocking. ~300ms then restores idle (except SLEEP)."""
        _C = {
            "VOL+":   (0, 12, 0),    # green
            "VOL-":   (12, 0, 0),    # red
            "STOP":   (12, 12, 12),  # white
            "LISTEN": (0, 0, 12),    # blue
            "WAKE":   (0, 12, 12),   # cyan
            "SLEEP":  (10, 5, 0),    # amber
            "MUTE":   (10, 0, 10),   # magenta
            "SKIP":   (12, 10, 0),   # yellow
        }
        r, g, b = _C.get(action, (8, 8, 8))

        def anim(action=action, r=r, g=g, b=b):
            if action in ("VOL+", "VOL-"):
                seq = range(self.n) if action == "VOL+" else range(self.n - 1, -1, -1)
                for i in seq:
                    if self._stop_anim.is_set(): return
                    px = [(0, 0, 0)] * self.n; px[i] = (r, g, b)
                    self._write(px); time.sleep(0.08)
                self._write([(0, 0, 0)] * self.n)
            elif action in ("LISTEN", "WAKE"):
                for _ in range(2):
                    if self._stop_anim.is_set(): return
                    self._write([(r, g, b)] * self.n); time.sleep(0.12)
                    self._write([(0, 0, 0)] * self.n);  time.sleep(0.08)
            elif action == "MUTE":
                for _ in range(3):
                    if self._stop_anim.is_set(): return
                    self._write([(r, g, b)] * self.n); time.sleep(0.10)
                    self._write([(0, 0, 0)] * self.n);  time.sleep(0.07)
            elif action == "SLEEP":
                for v in range(10, -1, -1):
                    if self._stop_anim.is_set(): return
                    self._write([(r * v // 10, g * v // 10, b * v // 10)] * self.n)
                    time.sleep(0.04)
            else:  # STOP, SKIP, fallback: single flash
                self._write([(r, g, b)] * self.n); time.sleep(0.25)
                self._write([(0, 0, 0)] * self.n)
            # restore idle after gesture (SLEEP lets the system handle its own transition)
            if not self._stop_anim.is_set() and action != "SLEEP":
                threading.Thread(target=self.show_idle, daemon=True).start()

        self._run_anim(anim)

    # ── Emotion-linked LED breathing ──────────────────────────────────────────

    _EMOTION_LED = {
        "NEUTRAL":   (0,  8,  8, 4.0, False),  # soft cyan, 4s
        "HAPPY":     (10, 8,  0, 3.0, False),  # warm yellow, 3s
        "CURIOUS":   (0,  10,10, 3.5, False),  # bright cyan, 3.5s
        "ANGRY":     (10,  0, 0, 2.0, False),  # red, 2s fast
        "SLEEPY":    (4,   0, 6, 6.0, False),  # dim purple, 6s slow
        "SURPRISED": (12, 12,12, 0.3, True),   # white flash → cyan
        "SAD":       (0,   0, 6, 6.0, False),  # dim blue, 6s
        "CONFUSED":  (8,   0, 8, 2.5, False),  # pulsing magenta, 2.5s
    }

    def show_emotion(self, emotion: str):
        # AMUSED: amber [255,160,0], sinusoidal breathe, floor=10 peak=80 period=1.5s gamma=1.8 duration=3s
        if emotion.upper() == "AMUSED":
            def anim():
                floor_ = 10; peak_ = 80; period_ = 1.5; gamma_ = 1.8; duration_ = 3.0
                dt = 0.04
                elapsed = 0.0
                while not self._stop_anim.is_set() and elapsed < duration_:
                    t = (elapsed % period_) / period_
                    s = (math.sin(2 * math.pi * t - math.pi / 2) + 1) / 2
                    v = int(floor_ + (peak_ - floor_) * (s ** gamma_))
                    self._write([(v, v * 160 // 255, 0)] * self.n)
                    time.sleep(dt)
                    elapsed += dt
                if not self._stop_anim.is_set():
                    self._write([(0, 0, 0)] * self.n)
            self._run_anim(anim)
            return

        cfg = self._EMOTION_LED.get(emotion.upper(), self._EMOTION_LED["NEUTRAL"])
        r, g, b, period, flash = cfg

        if flash:
            def anim():
                for _ in range(4):
                    if self._stop_anim.is_set(): return
                    self._write([(12, 12, 12)] * self.n); time.sleep(0.10)
                    if self._stop_anim.is_set(): return
                    self._write([(0, 0, 0)] * self.n);    time.sleep(0.08)
                steps = list(range(1, 9, 1))
                while not self._stop_anim.is_set():
                    for v in steps:
                        if self._stop_anim.is_set(): return
                        self._write([(0, v, v)] * self.n); time.sleep(0.04)
                    for v in reversed(steps):
                        if self._stop_anim.is_set(): return
                        self._write([(0, v, v)] * self.n); time.sleep(0.04)
            self._run_anim(anim)
        else:
            half = period / 2.0
            steps = max(10, int(half / 0.04))
            def anim(r=r, g=g, b=b, steps=steps, half=half):
                while not self._stop_anim.is_set():
                    for i in range(steps):
                        if self._stop_anim.is_set(): return
                        v = i / steps
                        self._write([(int(r * v), int(g * v), int(b * v))] * self.n)
                        time.sleep(half / steps)
                    for i in range(steps):
                        if self._stop_anim.is_set(): return
                        v = max(0.07, 1.0 - i / steps)
                        self._write([(int(r * v), int(g * v), int(b * v))] * self.n)
                        time.sleep(half / steps)
            self._run_anim(anim)

    def show_sleep(self):
        """Very dim indigo breathe for sleep mode. Re-reads iris_config.json each cycle
        so WebUI peak/floor/period/bright changes take effect without restarting the assistant."""
        import json as _json

        def _load():
            try:
                with open(_SLEEP_CFG_PATH) as _f:
                    _c = _json.load(_f)
            except Exception:
                _c = {}
            return (
                max(0, min(255, int(_c.get("LED_SLEEP_PEAK",   _SLEEP_DEFAULTS["LED_SLEEP_PEAK"])))),
                max(0, min(255, int(_c.get("LED_SLEEP_FLOOR",  _SLEEP_DEFAULTS["LED_SLEEP_FLOOR"])))),
                max(0.5, float(_c.get("LED_SLEEP_PERIOD", _SLEEP_DEFAULTS["LED_SLEEP_PERIOD"]))),
                max(0xE0, min(0xFF, int(_c.get("LED_SLEEP_BRIGHT", _SLEEP_DEFAULTS["LED_SLEEP_BRIGHT"])))),
            )

        def anim():
            while not self._stop_anim.is_set():
                peak, floor_, period, bright = _load()
                steps = 80
                for i in range(steps):
                    if self._stop_anim.is_set():
                        return
                    t = i / steps
                    s = (math.sin(2 * math.pi * t - math.pi / 2) + 1) / 2
                    v = floor_ + (peak - floor_) * s
                    self._write([(int(v * 0.5), 0, int(v))] * self.n, brightness=bright)
                    time.sleep(period / steps)
        self._run_anim(anim)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self):
        self.stop_anim()
        self.off()
        self.spi.close()
