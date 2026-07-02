#!/usr/bin/env python3
"""
tools/persona_harness/run_harness.py

Multi-turn persona + drift test harness for IRIS Ollama models.
Reuses production extract_emotion_from_reply, clean_llm_reply, and IntentRouter
by injecting pi4/ into sys.path. config.py will warn about missing
/home/pi/iris_config.json on non-Pi4 hosts — expected, falls back to defaults.

Usage (from repo root):
  python tools/persona_harness/run_harness.py [--model iris] [--tts] \
      [--script tools/persona_harness/turn_scripts/starter.txt] \
      [--output tools/persona_harness/reports/]
"""
import argparse
import json
import os
import sys
import time
import requests
from datetime import datetime

# ── sys.path: harness dir first, then pi4/ for production imports ─────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_PI4_PATH = os.path.join(_REPO_ROOT, "pi4")
for _p in (_HERE, _PI4_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Production imports ────────────────────────────────────────────────────────
# Shim note: config.py opens /home/pi/iris_config.json (FileNotFoundError caught,
# defaults used). intent_router.py tries os.makedirs /home/pi/logs/ (Exception
# caught, NullHandler assigned). Both import cleanly on Windows/SuperMaster.
from services.llm import extract_emotion_from_reply, clean_llm_reply  # noqa: E402
from core.intent_router import IntentRouter, ROUTE_LLM                # noqa: E402

# ── Harness-local modules ─────────────────────────────────────────────────────
from scorer import score_reply      # noqa: E402
from tts_client import kokoro_speak # noqa: E402

# ── Ollama connection ─────────────────────────────────────────────────────────
OLLAMA_HOST    = "192.168.1.3"
OLLAMA_PORT    = 11434
OLLAMA_TIMEOUT = 120  # seconds — conservative; mistral-small3.2:24b first-token ~2-3s warm

_DEFAULT_SCRIPT = os.path.join(_HERE, "turn_scripts", "starter.txt")
_DEFAULT_OUTPUT = os.path.join(_HERE, "reports")


# ── Ollama REST call ──────────────────────────────────────────────────────────

def _call_ollama(messages: list, model: str, num_predict: int = 350):
    """
    Non-streaming /api/chat. Returns (raw_content, latency_ms).
    Raises RuntimeError on any failure.
    """
    url = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": num_predict},
    }
    t0 = time.time()
    try:
        resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        latency_ms = round((time.time() - t0) * 1000)
        return content, latency_ms
    except Exception as e:
        raise RuntimeError(f"Ollama call failed: {e}") from e


# ── Turn script loader ────────────────────────────────────────────────────────

def _load_script(path: str):
    """
    Returns list-of-lists: each inner list is one conversation's utterances.
    '---' alone on a line starts a new conversation. '#' and blank lines skipped.
    """
    conversations = []
    current = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.strip() == "---":
                if current:
                    conversations.append(current)
                    current = []
            elif line.strip().startswith("#") or not line.strip():
                continue
            else:
                current.append(line.strip())
    if current:
        conversations.append(current)
    return conversations


# ── Report helpers ────────────────────────────────────────────────────────────

def _drift_assessment(flag_counts: dict, total_llm_turns: int) -> str:
    if total_llm_turns == 0:
        return "N/A"
    total_flags = sum(flag_counts.values())
    rate = total_flags / total_llm_turns
    if rate == 0:
        return "CLEAN"
    elif rate < 0.10:
        return "LOW"
    elif rate < 0.25:
        return "MEDIUM"
    return "HIGH"


def _write_summary(report: dict, path: str) -> None:
    s = report["summary"]
    r = report["run"]
    lines = [
        f"IRIS Persona Harness — model:{r['model']} — {r['started_at']}",
        "=" * 64,
        f"Script:           {r['script']}",
        f"Total turns:      {s['total_turns']}  (LLM: {s['total_llm_turns']})",
        f"TTS enabled:      {r['tts_enabled']}",
        f"Avg LLM latency:  {s['avg_latency_ms']} ms",
        f"Drift verdict:    {s['drift_assessment']}",
        "",
        "Flag totals:",
        f"  markdown_leak:        {s['flag_counts']['markdown_leak']}",
        f"  followup_boilerplate: {s['flag_counts']['followup_boilerplate']}",
        f"  rlhf_boilerplate:     {s['flag_counts']['rlhf_boilerplate']}",
        f"  persona_drift:        {s['flag_counts']['persona_drift']}",
        "",
        "Emotion distribution:",
    ]
    for em, count in sorted(s["emotion_distribution"].items(), key=lambda x: -x[1]):
        lines.append(f"  {em:<12} {count}")
    lines += ["", "Per-turn log:", "-" * 64]

    for t in report["turns"]:
        if not t.get("routed_to_llm"):
            lines.append(
                f"  Turn {t['turn_id']:3d} [C{t['conversation_id']}] "
                f"ROUTE:{t.get('intent_route','?')}:{t.get('intent_action','?')}  "
                f"\"{t['utterance']}\""
            )
            continue
        if t.get("error"):
            lines.append(f"  Turn {t['turn_id']:3d} [C{t['conversation_id']}] ERROR: {t['error']}")
            continue
        hit_flags = [k for k in ("markdown_leak", "followup_boilerplate", "rlhf_boilerplate", "persona_drift")
                     if t.get("flags", {}).get(k)]
        flags_str = ",".join(hit_flags) if hit_flags else "CLEAN"
        utt_preview = t["utterance"][:52]
        lines.append(
            f"  Turn {t['turn_id']:3d} [C{t['conversation_id']}] "
            f"{t['emotion']:<10} {t['latency_ms']:5d}ms  [{flags_str}]  "
            f"\"{utt_preview}\""
        )
        for detail in t.get("flags", {}).get("flag_details", []):
            lines.append(f"           >> {detail}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── Main run loop ─────────────────────────────────────────────────────────────

def run(model: str, script_path: str, output_dir: str, tts: bool) -> dict:
    router = IntentRouter()
    os.makedirs(output_dir, exist_ok=True)

    conversations = _load_script(script_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_base = os.path.join(output_dir, f"{model}_{ts}")

    turns_data = []
    global_turn = 0
    flag_counts = {"markdown_leak": 0, "followup_boilerplate": 0,
                   "rlhf_boilerplate": 0, "persona_drift": 0}
    emotion_dist = {}
    latencies = []

    print(f"[HARNESS] model={model}  script={os.path.basename(script_path)}  tts={tts}")
    print(f"[HARNESS] {len(conversations)} conversation(s) loaded\n")

    for conv_id, utterances in enumerate(conversations, 1):
        messages = []
        print(f"=== Conversation {conv_id} ({len(utterances)} turns) ===")

        for utt in utterances:
            global_turn += 1
            print(f"\n[T{global_turn:03d}] User: {utt}")

            intent = router.classify(utt)
            routed_to_llm = (intent.route == ROUTE_LLM)

            if not routed_to_llm:
                print(f"       -> ROUTE:{intent.route}:{intent.action}  (LLM skipped)")
                turns_data.append({
                    "turn_id": global_turn,
                    "conversation_id": conv_id,
                    "utterance": utt,
                    "intent_route": intent.route,
                    "intent_action": intent.action,
                    "routed_to_llm": False,
                    "raw_reply": intent.response or "",
                    "emotion": "NEUTRAL",
                    "cleaned_reply": intent.response or "",
                    "flags": score_reply(intent.response or "", intent.response or "", "NEUTRAL"),
                    "reply_len": len(intent.response or ""),
                    "latency_ms": 0,
                })
                continue

            messages.append({"role": "user", "content": utt})
            try:
                raw_reply, latency_ms = _call_ollama(messages, model)
            except RuntimeError as exc:
                print(f"       -> ERROR: {exc}")
                messages.pop()
                turns_data.append({
                    "turn_id": global_turn,
                    "conversation_id": conv_id,
                    "utterance": utt,
                    "intent_route": "LLM",
                    "intent_action": "LLM",
                    "routed_to_llm": True,
                    "raw_reply": f"ERROR: {exc}",
                    "emotion": "ERROR",
                    "cleaned_reply": "",
                    "flags": {},
                    "reply_len": 0,
                    "latency_ms": 0,
                    "error": str(exc),
                })
                continue

            emotion, text_after_tag = extract_emotion_from_reply(raw_reply)
            cleaned = clean_llm_reply(text_after_tag)
            flags = score_reply(raw_reply, cleaned, emotion)
            latencies.append(latency_ms)

            for k in flag_counts:
                if flags.get(k):
                    flag_counts[k] += 1
            emotion_dist[emotion] = emotion_dist.get(emotion, 0) + 1

            hit_flags = [k for k in flag_counts if flags.get(k)]
            print(f"       -> {emotion:<10}  {latency_ms}ms  flags={hit_flags or 'CLEAN'}")
            print(f"       -> {cleaned[:110]}{'...' if len(cleaned) > 110 else ''}")

            messages.append({"role": "assistant", "content": raw_reply})

            tts_path = None
            if tts:
                tts_path = os.path.join(output_dir, f"turn_{global_turn:03d}.wav")
                if not kokoro_speak(cleaned, tts_path):
                    tts_path = None

            rec = {
                "turn_id": global_turn,
                "conversation_id": conv_id,
                "utterance": utt,
                "intent_route": "LLM",
                "intent_action": "LLM",
                "routed_to_llm": True,
                "raw_reply": raw_reply,
                "emotion": emotion,
                "cleaned_reply": cleaned,
                "flags": flags,
                "reply_len": len(cleaned),
                "latency_ms": latency_ms,
            }
            if tts_path:
                rec["tts_audio"] = tts_path
            turns_data.append(rec)

    total_llm = sum(1 for t in turns_data if t.get("routed_to_llm"))
    report = {
        "run": {
            "model": model,
            "script": os.path.basename(script_path),
            "started_at": ts,
            "finished_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "total_turns": global_turn,
            "total_llm_turns": total_llm,
            "tts_enabled": tts,
        },
        "turns": turns_data,
        "summary": {
            "total_turns": global_turn,
            "total_llm_turns": total_llm,
            "flag_counts": flag_counts,
            "emotion_distribution": emotion_dist,
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
            "drift_assessment": _drift_assessment(flag_counts, total_llm),
        },
    }

    json_path = f"{report_base}_report.json"
    summary_path = f"{report_base}_summary.txt"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _write_summary(report, summary_path)

    print(f"\n{'='*64}")
    print(f"[HARNESS] COMPLETE — {global_turn} turns ({total_llm} LLM)")
    print(f"[HARNESS] Drift: {report['summary']['drift_assessment']}  "
          f"Flags: {sum(flag_counts.values())}")
    print(f"[HARNESS] JSON:    {json_path}")
    print(f"[HARNESS] Summary: {summary_path}")
    return report


def main():
    ap = argparse.ArgumentParser(description="IRIS persona/drift test harness")
    ap.add_argument("--model",  default="iris",            help="Ollama model (default: iris)")
    ap.add_argument("--tts",    action="store_true",       help="Save Kokoro audio per turn (wav)")
    ap.add_argument("--script", default=_DEFAULT_SCRIPT,   help="Turn script file")
    ap.add_argument("--output", default=_DEFAULT_OUTPUT,   help="Report output directory")
    args = ap.parse_args()

    if not os.path.exists(args.script):
        print(f"ERROR: script not found: {args.script}")
        sys.exit(1)

    run(args.model, args.script, args.output, args.tts)


if __name__ == "__main__":
    main()
