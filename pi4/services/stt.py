"""
services/stt.py - Speech-to-text via Wyoming Whisper
Connects to wyoming-whisper @ GANDALF:WHISPER_PORT.
Returns plain text string; empty string on failure.
"""

import json
import socket

from core.config import GANDALF, WHISPER_PORT, SAMPLE_RATE, CHANNELS
from services.wyoming import wy_send


def transcribe(audio_bytes: bytes) -> str:
    """Send raw PCM audio to Whisper STT, return transcript text."""
    with socket.create_connection((GANDALF, WHISPER_PORT), timeout=30) as s:
        wy_send(s, "transcribe", {"name": "", "language": "en"})
        wy_send(s, "audio-start", {"rate": SAMPLE_RATE, "width": 2, "channels": CHANNELS})
        for i in range(0, len(audio_bytes), 4096):
            wy_send(s, "audio-chunk",
                    {"rate": SAMPLE_RATE, "width": 2, "channels": CHANNELS},
                    audio_bytes[i:i + 4096])
        wy_send(s, "audio-stop", {})
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            lines = buf.split(b"\n")
            buf = lines[-1]
            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode())
                    if obj.get("type") == "transcript":
                        plen = obj.get("data_length", 0)
                        while len(buf) < plen:
                            buf += s.recv(4096)
                        try:
                            return json.loads(buf[:plen].decode()).get("text", "").strip()
                        except Exception:
                            return ""
                except json.JSONDecodeError:
                    pass
    return ""
