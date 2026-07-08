"""
services/vision.py - Camera capture and vision inference
Captures still image via rpicam-still, sends to Ollama vision model on GandalfAI.
"""

import base64
import os
import re
import subprocess
import tempfile
import time

import requests

from core.config import (
    CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_TIMEOUT,
    GANDALF, OLLAMA_PORT, VISION_MODEL, VISION_TRIGGERS,
)
from services.llm import extract_emotion_from_reply


def _vision_timeout() -> int:
    """Per-call read of VISION_TIMEOUT so a WebUI change takes effect without an
    assistant restart (same live-reload pattern KOKORO_SPEED uses). S194 Rung5."""
    from core.config import VISION_TIMEOUT
    return VISION_TIMEOUT


def capture_image() -> bytes | None:
    """Capture a JPEG from the Pi camera. Returns bytes or None on failure."""
    for attempt in range(1, 3):
        fd, tmp = tempfile.mkstemp(suffix='.jpg')
        os.close(fd)
        try:
            result = subprocess.run(
                ['rpicam-still', '-o', tmp,
                 '--width', str(CAMERA_WIDTH),
                 '--height', str(CAMERA_HEIGHT),
                 '--nopreview', '--immediate',
                 '-t', str(CAMERA_TIMEOUT)],
                capture_output=True,
                timeout=CAMERA_TIMEOUT / 1000 + 5,
            )
            if result.returncode != 0:
                err = result.stderr.decode(errors='replace')
                lines = [
                    l for l in err.splitlines()
                    if ' ERROR ' in l or ' WARN ' in l or (l and not l.startswith('['))
                ]
                diag = ' | '.join(lines[:4]) if lines else err[-300:]
                print(f"[CAM]  Capture failed (attempt {attempt}): {diag}", flush=True)
                if attempt < 2:
                    time.sleep(0.8)
                    continue
                return None
            with open(tmp, 'rb') as f:
                data = f.read()
            print(f"[CAM]  Captured {len(data) // 1024}KB (attempt {attempt})", flush=True)
            return data
        except Exception as e:
            print(f"[CAM]  Exception (attempt {attempt}): {e}", flush=True)
            if attempt < 2:
                time.sleep(0.8)
                continue
            return None
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return None


def is_vision_trigger(text: str) -> bool:
    """Return True if the transcribed text matches a vision trigger phrase."""
    return any(trigger in text.lower().strip().rstrip(".!?") for trigger in VISION_TRIGGERS)


# ── Kids camera games (S144) ──────────────────────────────────────────────────
_CAMERA_GAME_RE = {
    "FACE":    re.compile(r"guess my face|what face am i|guess my emotion|"
                          r"guess how i feel|read my face|make a face game|guess my mood"),
    "SHOW_ME": re.compile(r"guess what i'?m holding|guess what i have|guess what this is|"
                          r"guess this object|what am i holding|show ?me game|"
                          r"i'?m holding something"),
    "I_SPY":   re.compile(r"\bi spy\b|\beye spy\b|play i spy|i spy game"),
}
_CAMERA_GAME_GENERIC = re.compile(
    r"camera game|play a game with (?:the |your )?camera|looking game|"
    r"play a (?:looking|seeing|vision) game")


def classify_camera_game(text: str):
    """Return 'I_SPY' | 'SHOW_ME' | 'FACE' if the text requests a kids camera
    game, else None. Generic 'camera game' requests default to I Spy."""
    t = text.lower().strip()
    for game, rx in _CAMERA_GAME_RE.items():
        if rx.search(t):
            return game
    if _CAMERA_GAME_GENERIC.search(t):
        return "I_SPY"
    return None


def ask_vision_game(image_bytes: bytes, game_prompt: str, model: str) -> tuple:
    """Send image + a game-specific prompt to the given model. Unlike
    ask_vision(), this keeps the model's persona (kids model in kids mode) and
    its [EMOTION:X] tag so the face matches the game vibe. Sends only `prompt`
    (no `system` field) so the modelfile SYSTEM persona is preserved -- see
    [[ollama-system-message-overrides-modelfile]]. Returns (emotion, reply)."""
    img_b64 = base64.b64encode(image_bytes).decode()
    r = requests.post(
        f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
        json={
            "model": model,
            "prompt": game_prompt,
            "images": [img_b64],
            "stream": False,
            "options": {"num_ctx": 6144},
        },
        timeout=_vision_timeout(),
    )
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            return "HAPPY", "My camera game isn't working right now. Want to try something else?"
        raise
    data = r.json()
    reply = data.get("response", "") or data.get("message", {}).get("content", "")
    reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
    emotion, reply = extract_emotion_from_reply(reply)
    return (emotion or "HAPPY"), (reply or "Hmm, let's try that again!")


def ask_vision(image_bytes: bytes, prompt: str) -> str:
    """Send image + prompt to Ollama vision model. Returns plain text reply."""
    img_b64 = base64.b64encode(image_bytes).decode()
    vision_prompt = (
        f"Describe what you see in plain spoken sentences only. "
        f"No markdown, no lists, no preamble. 2-3 sentences max. "
        f"Describe objects, background, and setting. "
        f"If a person is visible, identify them by name if they resemble any of: "
        f"Leo (boy around age 9), Mae (girl around age 5), "
        f"Megan (adult woman), or Maestro (adult man). "
        f"Otherwise describe the person generically. "
        f"The user asked: {prompt}"
    )
    try:
        r = requests.post(
            f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
            json={
                "model": VISION_MODEL,
                "prompt": vision_prompt,
                "images": [img_b64],
                "stream": False,
                # mistral-small3.2:24b encodes a camera frame to ~4570 vision tokens,
                # which overflows the old 4096 default context window and returns
                # HTTP 400 "exceeds the available context size". num_ctx 6144 keeps
                # image+prompt+reply in context; matches the modelfile since S119b.
                "options": {"num_ctx": 6144},
            },
            timeout=_vision_timeout(),
        )
    except requests.exceptions.Timeout:
        # S194 Rung5: a hung/slow vision call no longer freezes the turn for 2 min.
        print(f"[VIS]  Vision POST timed out after {_vision_timeout()}s", flush=True)
        return "My eyes are running slow right now. Ask me again in a moment."
    except requests.exceptions.ConnectionError:
        print("[VIS]  Vision POST connection error -- GandalfAI unreachable", flush=True)
        return "I can't reach my eyes at the moment."
    try:
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            return "My vision system isn't available right now. The current AI model doesn't support images."
        raise
    data = r.json()
    reply = data.get("response", "") or data.get("message", {}).get("content", "")
    # Strip thinking blocks
    reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
    # Strip emotion tag -- vision uses same jarvis model which emits [EMOTION:X]
    _, reply = extract_emotion_from_reply(reply)
    return reply or "I could not make out what I was looking at."
