"""
tools/persona_harness/tts_client.py

Kokoro TTS client for the harness. Uses the verified OpenAI-compatible
/v1/audio/speech endpoint on GandalfAI.

Verified contract (inspected 2026-05-30 via docker + curl /openapi.json):
  POST http://192.168.1.3:8004/v1/audio/speech
  Schema: OpenAISpeechRequest
  Required:  input (str)
  Optional:
    model             str     default "kokoro"   (also accepts tts-1, tts-1-hd)
    voice             str     default "af_heart"  — production uses "bm_lewis"
    response_format   enum    mp3|opus|aac|flac|wav|pcm  default "mp3"
    speed             float   0.25–4.0  default 1.0
    stream            bool    default true  — harness uses false (save full file)
    return_download_link bool default false
    lang_code         str|null
    volume_multiplier float|null  default 1.0
    normalization_options  NormalizationOptions|null
  Returns: audio bytes with Content-Type matching response_format

Production voice/speed pulled from core.config: bm_lewis / 1.0
"""
import requests

KOKORO_URL   = "http://192.168.1.3:8004/v1/audio/speech"
KOKORO_VOICE = "bm_lewis"
KOKORO_SPEED = 1.0


def kokoro_speak(text: str, output_path: str,
                 voice: str = KOKORO_VOICE,
                 speed: float = KOKORO_SPEED) -> bool:
    """
    POST text to Kokoro, save WAV audio to output_path.
    Returns True on success, False on any failure (error printed to stdout).
    """
    payload = {
        "model":           "kokoro",
        "input":           text,
        "voice":           voice,
        "response_format": "wav",
        "speed":           speed,
        "stream":          False,
    }
    try:
        resp = requests.post(KOKORO_URL, json=payload, timeout=60)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as exc:
        print(f"[TTS]   kokoro_speak failed: {exc}", flush=True)
        return False
