"""
services/wakeword.py - Wake word detection via Wyoming OpenWakeWord
Listens on OWW_PORT for hey_jarvis detections.
Also handles GPIO button push-to-talk.

Returns "wake" (wakeword fired) or "button" (PTT button pressed).
"""

import json
import os
import select
import socket
import threading

from core.config import OWW_THRESHOLD, WAKE_WORD, SAMPLE_RATE, CHANNELS, CHUNK
from hardware.io import button_pressed
from services.wyoming import wy_send

_MANUAL_LISTEN_FLAG = "/tmp/iris_manual_listen"


def wait_for_wakeword_or_button(mic, oww_sock) -> str:
    """
    Block until wakeword detected above OWW_THRESHOLD or button pressed.
    Returns "wake" or "button".
    mic      — open PyAudio input stream (must be running)
    oww_sock — connected socket to wyoming-openwakeword
    """
    detected = threading.Event()
    trigger = [None]

    def reader():
        buf = b""
        while not detected.is_set():
            try:
                ready, _, _ = select.select([oww_sock], [], [], 0.05)
                if not ready:
                    continue
                chunk = oww_sock.recv(4096)
                if not chunk:
                    trigger[0] = "error"
                    detected.set()
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        hdr = json.loads(line.decode())
                        if hdr.get("type") == "detection":
                            data = hdr.get("data", {})
                            score = data.get("score", None)
                            if score is None:
                                scores = data.get("scores", {})
                                # Detection event = OWW already passed threshold; 1.0 if no score
                                score = max(scores.values()) if scores else 1.0
                            print(f"[OWW]  score={score:.3f} (threshold={OWW_THRESHOLD})",
                                  flush=True)
                            if score < OWW_THRESHOLD:
                                continue
                            trigger[0] = "wake"
                            detected.set()
                            return
                    except json.JSONDecodeError:
                        pass
            except Exception:
                break

    try:
        oww_sock.settimeout(5.0)
    except Exception:
        pass
    wy_send(oww_sock, "detect", {"names": [WAKE_WORD]})
    wy_send(oww_sock, "audio-start",
            {"rate": SAMPLE_RATE, "width": 2, "channels": CHANNELS})
    t = threading.Thread(target=reader, daemon=True)
    t.start()

    while not detected.is_set():
        audio = mic.read(CHUNK, exception_on_overflow=False)
        wy_send(oww_sock, "audio-chunk",
                {"rate": SAMPLE_RATE, "width": 2, "channels": CHANNELS},
                audio)
        if button_pressed() or os.path.exists(_MANUAL_LISTEN_FLAG):
            try: os.remove(_MANUAL_LISTEN_FLAG)
            except Exception: pass
            trigger[0] = "button"
            detected.set()
            threading.Event().wait(0.05)  # debounce

    t.join(timeout=2)
    if trigger[0] is None:
        trigger[0] = "error"
    return trigger[0] or "error"
