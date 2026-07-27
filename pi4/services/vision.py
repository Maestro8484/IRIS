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


def _vision_num_ctx() -> int:
    """S226: the vision calls must request the SAME num_ctx the modelfile sets.

    A per-request num_ctx that differs from the loaded one forces Ollama to
    unload and reload the whole 15 GB model -- measured at 10.0 s on GandalfAI.
    These three call sites used to hardcode 6144, which matched the modelfile
    by hand; once the modelfile moved to 12288 that hand-match would have made
    every camera turn pay a reload, and the following voice turn another one.
    Read live (not star-imported) so a config change needs no restart."""
    from core.config import LLM_NUM_CTX
    return LLM_NUM_CTX


def _vision_timeout() -> int:
    """Per-call read of VISION_TIMEOUT so a WebUI change takes effect without an
    assistant restart (same live-reload pattern KOKORO_SPEED uses). S194 Rung5."""
    from core.config import VISION_TIMEOUT
    return VISION_TIMEOUT


def _af_config() -> tuple:
    """Per-call read of the S216 autofocus settings, same live-reload pattern as
    _vision_timeout(). Returns (enabled, range, timeout_ms)."""
    from core.config import CAMERA_AUTOFOCUS, CAMERA_AF_RANGE, CAMERA_AF_TIMEOUT
    return CAMERA_AUTOFOCUS, CAMERA_AF_RANGE, CAMERA_AF_TIMEOUT


def capture_image() -> bytes | None:
    """Capture a JPEG from the Pi camera. Returns bytes or None on failure."""
    for attempt in range(1, 3):
        fd, tmp = tempfile.mkstemp(suffix='.jpg')
        os.close(fd)
        try:
            # S216: run a real autofocus cycle. --immediate captures instantly
            # and SKIPS AF entirely, which left the IMX708's lens parked at
            # whatever distance it last held -- anything near the camera came
            # back blurred. See the CAMERA_AUTOFOCUS block in core/config.py for
            # the evidence. Falls back to the exact pre-S216 command when the
            # flag is off, so a rollback is one config toggle.
            _af_on, _af_range, _af_ms = _af_config()
            cmd = ['rpicam-still', '-o', tmp,
                   '--width', str(CAMERA_WIDTH),
                   '--height', str(CAMERA_HEIGHT),
                   '--nopreview']
            if _af_on:
                cmd += ['--autofocus-mode', 'auto',
                        '--autofocus-range', _af_range,
                        '-t', str(_af_ms)]
                _budget = _af_ms / 1000 + 6
            else:
                cmd += ['--immediate', '-t', str(CAMERA_TIMEOUT)]
                _budget = CAMERA_TIMEOUT / 1000 + 5
            result = subprocess.run(cmd, capture_output=True, timeout=_budget)
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
    # S219: DRAW is matched BEFORE SHOW_ME on purpose. Dict order is iteration
    # order, and a child saying "guess what I drew" must not fall into the
    # hold-an-object game -- the two share most of their phrasing.
    "DRAW":    re.compile(r"guess my (?:drawing|picture|doodle)|guess what i drew|"
                          r"guess what i(?:'ve| have)? drawn|what did i draw|"
                          r"drawing game|i drew (?:something|a picture)|"
                          r"look at my (?:drawing|picture)"),
    "FACE":    re.compile(r"guess my face|what face am i|guess my emotion|"
                          r"guess how i feel|read my face|make a face game|guess my mood"),
    "SHOW_ME": re.compile(r"guess what i'?m holding|guess what i have|guess what this is|"
                          r"guess this object|what am i holding|show ?me game|"
                          r"i'?m holding something"),
    "I_SPY":   re.compile(r"\bi spy\b|\beye spy\b|play i spy|i spy game"),
    "RPS":     re.compile(r"rock,?\s+paper|paper,?\s+scissors|scissors\s+game|"
                          r"\brock scissors\b"),
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
            "options": {"num_ctx": _vision_num_ctx()},
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


# ── Game perception calls (S210) ─────────────────────────────────────────────
# These are DATA calls, not spoken output: the reply is parsed by
# core/camera_games and the verdict/lines are built in code. The LLM never
# judges a game. Still no `system` field (modelfile persona preserved, see
# ask_vision_game); the parsers tolerate persona preamble and [EMOTION:X] tags.

_ISPY_PICK_PROMPT = (
    "You are setting up a game of I Spy from this camera image. "
    "Pick exactly ONE object that is large, clearly visible, unmistakable, and that a "
    "young child would know the word for. Only pick something you are VERY confident "
    "about; prefer big obvious things (furniture, toys, clothing, appliances) over "
    "small or ambiguous ones. Reply with ONLY a JSON object, no other text, exactly: "
    '{"object": "<simple name>", "synonyms": ["<other names a child might call it>"], '
    '"color": "<main color>", "shape": "<shape or size as a simple adjective, e.g. round, square, tall>", '
    '"location": "<where it is in the room, e.g. on the table; never mention the image or photo>"}'
)

_RPS_HAND_PROMPT = (
    "The image shows a person playing rock-paper-scissors, holding one hand up to the "
    "camera. A closed fist is ROCK. A flat open hand is PAPER. Two extended fingers "
    "is SCISSORS. Reply with exactly one word: ROCK, PAPER, SCISSORS, or UNCLEAR if "
    "you cannot clearly see a hand gesture."
)


def _ask_vision_data(image_bytes: bytes, prompt: str, model: str, num_predict: int):
    """Shared one-shot vision POST for game data calls. Returns raw reply text
    or None on any failure (timeout, connection, HTTP) -- callers degrade
    gracefully, a game turn must never raise."""
    img_b64 = base64.b64encode(image_bytes).decode()
    try:
        r = requests.post(
            f"http://{GANDALF}:{OLLAMA_PORT}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
                "options": {"num_ctx": _vision_num_ctx(), "num_predict": num_predict},
            },
            timeout=_vision_timeout(),
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[GAME] vision data call failed: {e}", flush=True)
        return None
    data = r.json()
    reply = data.get("response", "") or data.get("message", {}).get("content", "")
    return re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()


def ask_ispy_pick(image_bytes: bytes, model: str):
    """One structured vision call: pick the I Spy object + kid-word synonyms +
    coarse attributes. Returns the validated pick dict or None."""
    from core.camera_games import parse_ispy_pick
    raw = _ask_vision_data(image_bytes, _ISPY_PICK_PROMPT, model, 220)
    if raw is None:
        return None
    pick = parse_ispy_pick(raw)
    if pick is None:
        print(f"[GAME] I Spy pick parse failed: {raw[:200]!r}", flush=True)
    return pick


def ask_rps_hand(image_bytes: bytes, model: str) -> str:
    """One-word hand classification: 'rock' | 'paper' | 'scissors' | 'unclear'."""
    from core.camera_games import parse_rps_reply
    raw = _ask_vision_data(image_bytes, _RPS_HAND_PROMPT, model, 60)
    if raw is None:
        return "unclear"
    move = parse_rps_reply(raw)
    if move == "unclear":
        print(f"[GAME] RPS hand unclear: {raw[:120]!r}", flush=True)
    return move


def ask_vision(image_bytes: bytes, prompt: str) -> str:
    """Send image + prompt to Ollama vision model. Returns plain text reply."""
    img_b64 = base64.b64encode(image_bytes).decode()
    # S216: the household name roster was REMOVED. It forced an identification the
    # model cannot make -- it has no face recognition, so a "resembles any of"
    # list is a forced-choice guess dressed up as recognition. Live proof
    # (journal 2026-07-20 18:57:41), an adult bearded male at the camera:
    # "A child standing in front of a microwave ... It's the boy named Ben."
    # Two minutes later the same camera got him right ("an adult male with blue
    # eyes and facial hair") precisely because that reply did not take the naming
    # bait. Name-blindness here also matches the S201B kids fix. The
    # only-what-you-can-see clause targets the second half of that failure (the
    # invented microwave): the model narrated a confident scene it could not
    # actually resolve.
    vision_prompt = (
        f"Describe what you see in plain spoken sentences only. "
        f"No markdown, no lists, no preamble. 2-3 sentences max. "
        f"Describe objects, background, and setting. "
        f"Describe any person only by what you can actually see, for example "
        f"'a man with a beard' or 'a young girl'. Never state or guess anyone's name: "
        f"you cannot recognize faces, and a confident wrong name is worse than no name. "
        f"Only mention things you can clearly make out. If an object is unclear, leave it "
        f"out rather than guessing what it might be. "
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
                # HTTP 400 "exceeds the available context size". Must match the
                # modelfile or every camera turn reloads the model (S226).
                "options": {"num_ctx": _vision_num_ctx()},
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
