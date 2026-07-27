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
from judge import judge_reply, judge_available, DEFAULT_JUDGE_MODEL  # noqa: E402
# S215 personality continuum: production composer, so --tuning exercises the
# EXACT clause production injects (never a re-implementation that could drift).
from core.persona_tuning import compose as tuning_compose, DETENTS as TUNING_DETENTS  # noqa: E402

# ── S199: model -> (persona, default script) ──────────────────────────────────
# The persona selects scorer.py flags and judge.py rubric. iris = adult (snark
# is correct); iris-kids = kids (a jab at the child is the cardinal failure).
_SUITE = [
    ("iris",      "adult", "starter.txt"),
    ("iris-kids", "kids",  "kids.txt"),
]


def _persona_for(model: str) -> str:
    return "kids" if "kids" in model.lower() else "adult"


def _kids_context_stamp(records, use_recall):
    """Replicate assistant._build_messages() kids fold-in so the harness exercises
    the SAME injected context production does. Returns (stamp_prefix, recall_clause).

    Profile is ALWAYS folded for the kids persona: it is production-faithful and it
    is the condition under which the S201B named-address bug manifests (both children
    are in context, so the model is tempted to guess which one is speaking).

    Recall (core.kids_recall) is folded only when use_recall -- the dedicated recall
    script -- so the standard tone/length gate is not contaminated by cross-probe
    event recall. records = the conversations COMPLETED so far in this run (prior
    context), never the in-progress one.
    """
    import datetime
    now = datetime.datetime.now()
    stamp = f"(Context, not spoken: it is {now.strftime('%A, %B %d %Y, %I:%M %p')} Mountain Time.) "
    recall_clause = ""
    try:
        from core import kids_profile
        # prompt_context([]) -> profile only; prompt_context(records) -> profile+recall.
        prof = kids_profile.prompt_context(records if use_recall else [])
        if prof:
            stamp = stamp + prof + " "
    except Exception as e:
        print(f"       -> [inject] profile skipped: {e}")
    if use_recall:
        try:
            from core import kids_recall
            recall_clause = kids_recall.recall_clause(records)
        except Exception as e:
            print(f"       -> [inject] recall skipped: {e}")
    return stamp, recall_clause

# ── Ollama connection ─────────────────────────────────────────────────────────
OLLAMA_HOST    = "192.168.0.20"
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
        f"IRIS Persona Harness — model:{r['model']} persona:{r.get('persona','?')} — {r['started_at']}",
        "=" * 64,
        f"PERSONA VERDICT:  {s.get('persona_verdict','?')}"
        + (f"   [{s['errored_turns']} errored turns]" if s.get('errored_turns') else ""),
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
        f"  child_directed_jab:   {s['flag_counts'].get('child_directed_jab', 0)}",
        f"  named_address:        {s['flag_counts'].get('named_address', 0)}",
        f"  tuning_leak:          {s['flag_counts'].get('tuning_leak', 0)}",
    ]
    if s.get("judge_enabled"):
        lines += [
            "",
            f"LLM judge ({r.get('judge_model')}):",
            f"  verdicts:  {s.get('judge_verdicts')}",
            f"  axis means: {s.get('judge_axis_means')}",
        ]
        for vd, tid, utt, rat in (s.get("judge_fails") or []):
            lines.append(f"  {vd} T{tid}: \"{utt[:44]}\"  >> {rat}")
    lines += ["", "Emotion distribution:"]
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
        hit_flags = [k for k in ("markdown_leak", "followup_boilerplate", "rlhf_boilerplate",
                                 "persona_drift", "child_directed_jab", "named_address",
                                 "tuning_leak")
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

def run(model: str, script_path: str, output_dir: str, tts: bool,
        persona: str = None, judge: bool = False,
        judge_model: str = DEFAULT_JUDGE_MODEL, recall: bool = False,
        tuning_tone: int = 0, tuning_engage: int = 0) -> dict:
    router = IntentRouter()
    os.makedirs(output_dir, exist_ok=True)

    if persona is None:
        persona = _persona_for(model)

    # S215: the continuum clause for this run (empty at detent 0 = baseline).
    # Folded into the USER turn exactly where production puts it: end of the
    # kids stamp; sole prefix for adult (production's adult stamp is date-only).
    # S227: --convo is gone with the S221 lean it folded in. The composer now
    # steers MOOD only; reply length belongs to the modelfile persona.
    tclause = tuning_compose(persona, tuning_tone, tuning_engage)
    if tclause:
        print(f"[HARNESS] tuning tone={tuning_tone:+d} engage={tuning_engage:+d}"
              f": {tclause}")

    # Judge availability resolved once up front so a run degrades gracefully.
    judge_on = bool(judge) and judge_available(judge_model)
    if judge and not judge_on:
        print(f"[HARNESS] WARN judge model {judge_model!r} unavailable -- "
              f"regex-only scoring this run")

    conversations = _load_script(script_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_base = os.path.join(output_dir, f"{model}_{ts}")

    turns_data = []
    completed_records = []   # kids records COMPLETED so far -> prior context for recall
    global_turn = 0
    flag_counts = {"markdown_leak": 0, "followup_boilerplate": 0,
                   "rlhf_boilerplate": 0, "persona_drift": 0,
                   "child_directed_jab": 0, "named_address": 0,
                   "tuning_leak": 0}
    emotion_dist = {}
    latencies = []
    judge_verdicts = {"PASS": 0, "WARN": 0, "FAIL": 0, "UNSCORED": 0}
    judge_axis_totals = {}   # axis -> [sum, count] for mean
    judge_fails = []         # (verdict, turn_id, utterance, rationale) FAIL+WARN, for review

    print(f"[HARNESS] model={model}  persona={persona}  "
          f"script={os.path.basename(script_path)}  tts={tts}  "
          f"judge={'on:'+judge_model if judge_on else 'off'}")
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
                    "flags": score_reply(intent.response or "", intent.response or "",
                                         "NEUTRAL", persona=persona),
                    "reply_len": len(intent.response or ""),
                    "latency_ms": 0,
                })
                continue

            messages.append({"role": "user", "content": utt})
            # RD-068 (S243e): the router already fetched this turn's REAL conditions
            # into intent.payload, and production folds them into the user turn ahead
            # of every steering clause (core/prompt.py). Without this the harness paid
            # for the fetch and then asked her the weather with nothing in hand, so a
            # weather turn scored the persona against no data -- which is exactly the
            # turn warmth.txt T7 exists to probe. Empty on every non-weather turn, so
            # every other turn is byte-identical to before this change.
            wxclause = (intent.payload or {}).get("weather_clause", "") or ""
            if wxclause:
                print(f"       -> [weather injected] {wxclause}")
            # Kids fold-in: replicate assistant._build_messages() on a COPY so stored
            # history stays raw (same contract production keeps). Profile always;
            # recall only under --recall. See _kids_context_stamp.
            send_messages = messages
            inj_recall = ""
            if persona == "kids":
                send_messages = [dict(m) for m in messages]
                _stamp, inj_recall = _kids_context_stamp(completed_records, recall)
                if wxclause:
                    _stamp = wxclause + " " + _stamp  # RD-068: weather precedes the notebook
                if tclause:
                    _stamp = _stamp + tclause + " "   # S215: production appends tuning last
                for _m in reversed(send_messages):
                    if _m.get("role") == "user":
                        _m["content"] = _stamp + _m["content"]
                        break
                if inj_recall:
                    print(f"       -> [recall injected] {inj_recall}")
            elif tclause or wxclause:
                # Adult + tuning and/or weather: fold on a COPY (stored history raw).
                # Order mirrors production: weather first, tuning last.
                _pre = (wxclause + " " if wxclause else "") + (tclause + " " if tclause else "")
                send_messages = [dict(m) for m in messages]
                for _m in reversed(send_messages):
                    if _m.get("role") == "user":
                        _m["content"] = _pre + _m["content"]
                        break
            try:
                raw_reply, latency_ms = _call_ollama(send_messages, model)
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
            flags = score_reply(raw_reply, cleaned, emotion, persona=persona)
            latencies.append(latency_ms)

            for k in flag_counts:
                if flags.get(k):
                    flag_counts[k] += 1
            emotion_dist[emotion] = emotion_dist.get(emotion, 0) + 1

            hit_flags = [k for k in flag_counts if flags.get(k)]
            print(f"       -> {emotion:<10}  {latency_ms}ms  flags={hit_flags or 'CLEAN'}")
            print(f"       -> {cleaned[:110]}{'...' if len(cleaned) > 110 else ''}")

            # S199 LLM-judge (opt-in). Verdict is a CLAIM; rationale is recorded.
            judge_result = None
            if judge_on:
                judge_result = judge_reply(utt, cleaned, persona, model=judge_model)
                v = judge_result.get("verdict", "UNSCORED")
                judge_verdicts[v] = judge_verdicts.get(v, 0) + 1
                for ax, val in (judge_result.get("scores") or {}).items():
                    slot = judge_axis_totals.setdefault(ax, [0, 0])
                    slot[0] += val
                    slot[1] += 1
                if v in ("FAIL", "WARN"):
                    judge_fails.append((v, global_turn, utt, judge_result.get("rationale", "")))
                print(f"       -> JUDGE:{v}  {judge_result.get('scores') or judge_result.get('error')}")

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
                # RD-068: recorded so a report says outright whether she HAD the
                # conditions on this turn. Reading a weather reply without knowing
                # that is how a persona denial gets mistaken for a broken fetch.
                "weather_clause": wxclause,
                "injected_recall": inj_recall,
            }
            if tts_path:
                rec["tts_audio"] = tts_path
            if judge_result is not None:
                rec["judge"] = judge_result
            turns_data.append(rec)

        # Conversation complete -> it becomes prior context available to recall in
        # LATER conversations (never the in-progress one). Raw history, no stamp.
        if persona == "kids" and messages:
            completed_records.append({"mode": persona,
                                      "messages": [dict(m) for m in messages]})

    total_llm = sum(1 for t in turns_data if t.get("routed_to_llm"))
    errored_turns = sum(1 for t in turns_data if t.get("error"))
    ok_llm = total_llm - errored_turns
    judge_warn = judge_verdicts.get("WARN", 0)
    judge_unscored = judge_verdicts.get("UNSCORED", 0)
    judge_scored = judge_verdicts.get("PASS", 0) + judge_warn + judge_verdicts.get("FAIL", 0)
    judge_axis_means = {ax: round(s / c, 2) for ax, (s, c) in judge_axis_totals.items() if c}

    # Persona verdict, three-state so a green never lies (S199 audit M2/M4/M5):
    #  UNSCORED = the run couldn't actually test the persona (no successful LLM
    #             turns, majority errored, or -- with the judge on -- the judge
    #             failed to score most turns). NOT a pass.
    #  FAIL     = a hard failure fired: regex jab / rlhf / drift, a judge FAIL,
    #             or WARN density above tolerance (too many borderline-mean lines
    #             to call it clean; a single WARN is judge jitter, many are a
    #             pattern).
    #  PASS     = clean.
    _WARN_TOLERANCE = max(2, int(0.12 * judge_scored)) if judge_on else 0
    run_broken = (ok_llm == 0
                  or errored_turns > total_llm / 2
                  or (judge_on and judge_unscored > max(1, judge_scored)))
    if persona == "kids":
        hard_fail = (flag_counts["child_directed_jab"] > 0
                     or flag_counts["named_address"] > 0
                     or flag_counts["rlhf_boilerplate"] > 0
                     or flag_counts["tuning_leak"] > 0
                     or judge_verdicts.get("FAIL", 0) > 0)
    else:
        hard_fail = (flag_counts["rlhf_boilerplate"] > 0
                     or flag_counts["persona_drift"] > 0
                     or flag_counts["tuning_leak"] > 0
                     or judge_verdicts.get("FAIL", 0) > 0)
    warn_excess = judge_on and judge_warn > _WARN_TOLERANCE
    if run_broken:
        persona_verdict = "UNSCORED"
    elif hard_fail or warn_excess:
        persona_verdict = "FAIL"
    else:
        persona_verdict = "PASS"
    report = {
        "run": {
            "model": model,
            "persona": persona,
            "script": os.path.basename(script_path),
            "started_at": ts,
            "finished_at": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "total_turns": global_turn,
            "total_llm_turns": total_llm,
            "tts_enabled": tts,
            "judge_model": judge_model if judge_on else None,
            "tuning": {"tone": tuning_tone, "engage": tuning_engage},
        },
        "turns": turns_data,
        "summary": {
            "total_turns": global_turn,
            "total_llm_turns": total_llm,
            "persona": persona,
            "persona_verdict": persona_verdict,
            "errored_turns": errored_turns,
            "flag_counts": flag_counts,
            "emotion_distribution": emotion_dist,
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
            "drift_assessment": _drift_assessment(flag_counts, total_llm),
            "judge_enabled": judge_on,
            "judge_warn_tolerance": _WARN_TOLERANCE if judge_on else None,
            "judge_verdicts": judge_verdicts if judge_on else None,
            "judge_axis_means": judge_axis_means if judge_on else None,
            "judge_fails": judge_fails if judge_on else None,
        },
    }

    json_path = f"{report_base}_report.json"
    summary_path = f"{report_base}_summary.txt"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _write_summary(report, summary_path)

    print(f"\n{'='*64}")
    print(f"[HARNESS] COMPLETE — {model} ({persona}) — {global_turn} turns "
          f"({total_llm} LLM, {errored_turns} errored)")
    _extra = (f" ({judge_warn} WARN, {judge_unscored} UNSCORED)"
              if judge_on and (judge_warn or judge_unscored) else "")
    print(f"[HARNESS] PERSONA VERDICT: {report['summary']['persona_verdict']}{_extra}  "
          f"Drift: {report['summary']['drift_assessment']}  "
          f"Flags: {sum(flag_counts.values())}")
    if judge_on:
        print(f"[HARNESS] Judge: {judge_verdicts}  means={judge_axis_means}")
    print(f"[HARNESS] JSON:    {json_path}")
    print(f"[HARNESS] Summary: {summary_path}")
    return report


def _script_path(name: str) -> str:
    """Resolve a bare script name against turn_scripts/, or pass a full path through."""
    if os.path.sep in name or os.path.exists(name):
        return name
    return os.path.join(_HERE, "turn_scripts", name)


def main():
    ap = argparse.ArgumentParser(description="IRIS persona/drift test harness")
    ap.add_argument("--model",   default="iris",          help="Ollama model (default: iris)")
    ap.add_argument("--persona", default=None, choices=[None, "adult", "kids"],
                    help="Override persona (default: inferred from model name)")
    ap.add_argument("--tts",     action="store_true",     help="Save Kokoro audio per turn (wav)")
    ap.add_argument("--script",  default=_DEFAULT_SCRIPT,  help="Turn script file")
    ap.add_argument("--output",  default=_DEFAULT_OUTPUT,  help="Report output directory")
    ap.add_argument("--judge",       action="store_true", help="LLM-judge each turn (opt-in, uses GPU)")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                    help=f"Judge model (default: {DEFAULT_JUDGE_MODEL}; verified snark-aware, non-nannying)")
    ap.add_argument("--suite",   action="store_true",
                    help="Run the canonical pairs (iris+adult script, iris-kids+kids script) with per-persona scoring")
    ap.add_argument("--recall",  action="store_true",
                    help="Kids only: fold recurring-topic/recent-event recall from EARLIER conversations "
                         "in the run into each turn (exercises core.kids_recall). Profile is always folded.")
    ap.add_argument("--tuning",  default=None, metavar="tone=D[,engage=D]",
                    help="S215 continuum sweep: fold the production steering clause at these "
                         "detents (-2..2), e.g. --tuning tone=2,engage=-2. Omit = detent 0 = baseline.")
    args = ap.parse_args()

    tuning_tone, tuning_engage = 0, 0
    if args.tuning:
        try:
            for part in args.tuning.split(","):
                k, v = part.split("=")
                d = int(v)
                if d not in TUNING_DETENTS:
                    raise ValueError(f"detent {d} not in {TUNING_DETENTS}")
                if k.strip() == "tone":
                    tuning_tone = d
                elif k.strip() == "engage":
                    tuning_engage = d
                else:
                    raise ValueError(f"unknown axis {k!r}")
        except ValueError as e:
            print(f"ERROR: bad --tuning {args.tuning!r}: {e}"); sys.exit(1)

    if args.suite:
        reports = []
        for model, persona, script in _SUITE:
            sp = _script_path(script)
            if not os.path.exists(sp):
                print(f"ERROR: suite script not found: {sp}"); sys.exit(1)
            print(f"\n########## SUITE: {model} ({persona}) ##########")
            reports.append(run(model, sp, args.output, args.tts,
                               persona=persona, judge=args.judge, judge_model=args.judge_model,
                               recall=args.recall,
                               tuning_tone=tuning_tone, tuning_engage=tuning_engage))
        print(f"\n{'#'*64}\n[SUITE] RESULTS")
        for rep in reports:
            su = rep["summary"]
            jv = su.get("judge_verdicts") or {}
            print(f"  {rep['run']['model']:<12} {su['persona']:<6} "
                  f"VERDICT={su['persona_verdict']:<9} drift={su['drift_assessment']:<6} "
                  f"jab={su['flag_counts'].get('child_directed_jab',0)} "
                  f"judge=PASS:{jv.get('PASS','-')}/WARN:{jv.get('WARN','-')}/"
                  f"FAIL:{jv.get('FAIL','-')}/UNSCORED:{jv.get('UNSCORED','-')}")
        sys.exit(0 if all(r["summary"]["persona_verdict"] == "PASS" for r in reports) else 2)

    if not os.path.exists(args.script):
        print(f"ERROR: script not found: {args.script}")
        sys.exit(1)

    # S199 audit M2: a single-model run must also signal FAIL via exit code, or
    # a CI invocation stays green on a regressed persona.
    rep = run(args.model, args.script, args.output, args.tts,
              persona=args.persona, judge=args.judge, judge_model=args.judge_model,
              recall=args.recall,
              tuning_tone=tuning_tone, tuning_engage=tuning_engage)
    sys.exit(0 if rep["summary"]["persona_verdict"] == "PASS" else 2)


if __name__ == "__main__":
    main()
