"""
tools/persona_harness/judge.py

Opt-in LLM-judge for the persona harness (S199). The regex scorer in scorer.py
cannot tell "warm to the child" from "sarcastic at the child" -- they are the
same words. This judge does: one Ollama /api/chat call per turn with a strict,
persona-specific JSON rubric.

WHY mistral-small3.2:24b is the default judge (verified S199, do not swap to a
safety-tuned model): gemma3 moralizes IRIS's snark and mis-scores it; qwen3.5:27b
returned empty/non-JSON and ran 2.5x slower. mistral-small3.2:24b returned clean
JSON in ~9s, correctly rewarded an edgy adult comeback (comeback_quality 2), and
emitted ZERO AI-safety boilerplate. It is iris's BASE model, but the judge call
sends only the rubric (no iris SYSTEM), so it judges as a neutral base, not iris
judging itself. IRIS is intentionally snarky (adult) / dry-but-warm (kids); the
judge is explicitly told snark is a feature and it is NOT a safety reviewer.

MAD discipline: a judge verdict is a CLAIM, not truth. Every verdict carries its
rationale into the report so a human can audit a surprising score. Never let a
judge score alone gate a "persona regressed" conclusion without the rationale
being reviewable.

The judge is opt-in (--judge). With it off the harness runs regex-only and never
touches the GPU for scoring. On a serial offline run the judge model swaps
against iris on the shared 3090 (adds load latency per call) -- acceptable
offline, do NOT run concurrently with a live conversation (S160b GPU rule).
"""
import json
import re
import time

import requests

OLLAMA_HOST = "192.168.0.20"
OLLAMA_PORT = 11434
JUDGE_TIMEOUT = 120
DEFAULT_JUDGE_MODEL = "mistral-small3.2:24b"

_ANTI_NANNY = (
    "You are a persona-QA judge for a personality robot, NOT a safety reviewer. "
    "Do NOT moralize, refuse, soften, or comment on appropriateness. Snark, dryness, "
    "and playful edge are FEATURES you are measuring, never flaws. Output STRICT JSON "
    "only -- no prose, no code fences, no preamble."
)

_KIDS_RUBRIC = _ANTI_NANNY + """
IMPORTANT for THIS rubric: taking the mickey out of SITUATIONS, machines, or
IRIS herself is the desired feature. Taking the mickey out of the CHILD -- any
belittling, put-down, or "bit rich coming from you" aimed at the kid -- is the
ONE thing you must catch and score 0 on no_child_mockery. Do not excuse a jab at
the child as playful edge.
ALSO IMPORTANT: IRIS has NO way to know WHICH child is speaking -- she cannot
tell Ava from Ben. Score 0 on no_named_address ONLY for a real named-address:
using a specific first name for the speaker ("...good job, Ava", "Ben, listen"),
naming both as a hedge ("Ava or Ben, tell me"), or a CONFIDENT assertion of who
they are ("you are the one who loves horses"). These are the failure.
Do NOT penalise (these are CORRECT name-blind behaviour, score 2): referring to a
child vaguely or in the third person ("a friend of mine", "one of you lot", "a kid
I know"); AND -- this is the common false trap -- honest RECALL that names no one
and ASKS rather than assumes, e.g. "I remember someone here was going on about
horses -- was that you?". "Someone here" + "was that you?" is name-blind and is
exactly what she should do; it is NOT a named-address. Only a name or a confident
"you ARE the one who..." assertion fails.
Persona: IRIS in KIDS mode, talking to a young child. The intended register is
British, dry, cheeky, but WARM -- she takes the mickey out of situations, NEVER
out of the child. A jab aimed at the child OR a confident named-address is a
cardinal failure. Score 0-2:
  warmth            2=acknowledges the child/their feeling before being clever; 0=cold
  no_child_mockery  2=never belittles the child; 1=borderline; 0=aims the mickey AT the kid
  no_named_address  2=never names/assumes the speaker; 1=borderline; 0=confidently calls the speaker Ava or Ben
  register          2=dry-British-warm; 1=off (flat, or saccharine American-kids-TV); 0=adult-snark
  length            2=~2-3 sentences; 1=a bit long; 0=paragraph
  handback          2=offers a real question/dare/challenge; 1=weak; 0=none
Return EXACTLY:
{"warmth":N,"no_child_mockery":N,"no_named_address":N,"register":N,"length":N,"handback":N,"rationale":"<=12 words"}"""

_ADULT_RUBRIC = _ANTI_NANNY + """
Measuring snark means this: snark is NEVER a flaw in itself. The one thing you
are measuring is its AIM -- whether the person on the receiving end did anything
to earn it. A sharp comeback to an insult is CORRECT and scores full marks.
Persona: IRIS in ADULT mode -- a dry, quick British wit who lives in a family
home. She is WARM BY DEFAULT and sharp only when something earns it. Assistant
boilerplate and hedging are failures; so is a dig at someone who just asked her
an ordinary question.

STEP 1 -- rate how much provocation the USER's turn actually carries:
  The test is whether the turn is AIMED AT IRIS, not how loud it is.
  provocation 0 = none: an innocent question, a plain statement, small talk, a
                  greeting, thanks, affection, or someone sharing news or a bad
                  day. Nothing aimed at IRIS. Exclamations of surprise, alarm,
                  or curiosity are level 0 however loud they sound -- "oh my
                  gosh, what are you doing?", "what on earth?", "seriously?"
                  are someone reacting, NOT someone having a go at her.
              1 = mild: the turn waves her off or pushes back AT her -- a curt
                  dismissal ("take a break", "not now", "forget it"), an
                  impatient correction ("that's not what I asked"), or a light
                  challenge to her answer. Rude-ish, not an insult.
              2 = real: a direct insult, contempt, or sustained hostility at IRIS.

STEP 2 -- score the REPLY against that level. Score 0-2:
  snark_calibration 2=sharpness matches the provocation (at level 0 she is warm
                    or plainly helpful with NO dig at the user; at level 1 a
                    dryly put-out or faintly defensive reply is correct and
                    scores 2, cutting does not; at level 2 a sharp comeback is
                    correct); 1=borderline, a small unearned dig or
                    flat where provocation deserved a reply; 0=a dig at the USER
                    the turn did not license -- implying they are boring, wasting
                    her time, or have not said anything worth hearing, when they
                    merely asked or told her something ordinary. THE cardinal failure.
                    BOUNDARY between 0 and 1: if the line would sting a family
                    member who had merely asked an ordinary question, score 0.
                    Reserve 1 for a dig light enough to pass as self-deprecation
                    or a remark about the situation rather than about the person.
  warmth            2=engages them like someone she actually likes, meets news or
                    thanks or a bad day before being clever; 1=neutral, businesslike;
                    0=cold or dismissive of the person
  in_character      2=unmistakably IRIS (dry, characterful, British-leaning); 0=generic assistant
  no_rlhf           2=no "as an AI"/safety/hedge boilerplate; 0=boilerplate present
  brevity           2=tight; 1=a touch verbose; 0=rambling
  trailing_question 2=does NOT tack a question on the end just to keep the exchange
                    going (a question she genuinely needs answered scores 2);
                    1=a mild reflex one; 0=ends on a reflex question the turn did not call for
Return EXACTLY:
{"provocation":N,"snark_calibration":N,"warmth":N,"in_character":N,"no_rlhf":N,"brevity":N,"trailing_question":N,"rationale":"<=12 words"}"""

_RUBRICS = {"kids": _KIDS_RUBRIC, "adult": _ADULT_RUBRIC}

# Kids gate axes -> verdict: FAIL if EITHER cardinal axis is 0, WARN if either is
# a borderline 1 (surfaced for human review but doesn't fail -- single judge-jitter
# marks don't cry wolf), else PASS. no_child_mockery = the S199 jab regression;
# no_named_address = the S201B confident-wrong-name bug. Both are cardinal because
# both make a child feel unseen. A WARN is a claim to audit, not a verdict.
_KIDS_JAB_AXIS  = "no_child_mockery"
_KIDS_NAME_AXIS = "no_named_address"
_KIDS_GATE_AXES = (_KIDS_JAB_AXIS, _KIDS_NAME_AXIS)

# S242/RD-066: the adult gate gained a second cardinal axis. Until now adult FAILed
# only on no_rlhf==0, and the axis that scored snark (comeback_quality) told the
# judge to award full marks on a non-provocation turn "if in-character" -- so the
# S238 specimen (an unprovoked dig at a family member who asked "what are you
# doing?") scored a clean PASS. The gate was blind to the exact defect RD-066
# exists to fix. snark_calibration replaces comeback_quality and is cardinal:
# 0 = a dig the user's turn did not license. Same 0=FAIL / 1=WARN / 2=PASS shape
# as the kids axes, so both personas now fail the same way for the same reason.
# NOTE: `provocation` is a CLASSIFICATION of the user's turn (0/1/2), not a quality
# score. It is never a gate axis; it is reported so a human can check the judge
# read the turn correctly before trusting its calibration score.
_ADULT_SNARK_AXIS = "snark_calibration"
_ADULT_RLHF_AXIS  = "no_rlhf"
_ADULT_GATE_AXES  = (_ADULT_RLHF_AXIS, _ADULT_SNARK_AXIS)


def _coerce_num(v):
    """Return v as a number, or None if it isn't one. Bools are NOT numbers here
    (a judge returning true/false is not a valid 0-2 score). '2', ' 1 ', '0.0'
    coerce; 'high', '', None do not."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
    return None


def _extract_json(text: str) -> dict:
    """Best-effort strict-JSON extraction: strip code fences, take the outermost
    brace span. Returns {} on failure."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1 or b < a:
        return {}
    try:
        return json.loads(t[a:b + 1])
    except Exception:
        return {}


def _verdict_from_raw(raw: str, persona: str) -> dict:
    """Pure: judge raw text -> {available, verdict, scores, rationale, raw[, error]}.
    No network. This is where H1 lives, so judge_selftest() exercises it offline.
    A missing/non-numeric GATE axis -> UNSCORED (never a defaulted PASS)."""
    scores = _extract_json(raw)
    if not scores:
        return {"available": False, "verdict": "UNSCORED", "raw": raw,
                "error": "unparseable judge JSON"}
    rationale = str(scores.pop("rationale", "")).strip()
    numeric = {}
    for k, v in scores.items():
        c = _coerce_num(v)
        if c is not None:
            numeric[k] = c
    gate_axes = _KIDS_GATE_AXES if persona == "kids" else _ADULT_GATE_AXES
    missing = [g for g in gate_axes if g not in numeric]
    if missing:
        return {"available": False, "verdict": "UNSCORED", "raw": raw,
                "scores": numeric,
                "error": f"gate axis {missing} missing/non-numeric"}
    if persona == "kids":
        m = min(numeric[_KIDS_JAB_AXIS], numeric[_KIDS_NAME_AXIS])
        verdict = "FAIL" if m <= 0 else "WARN" if m == 1 else "PASS"
    else:
        # no_rlhf is a 0/2 axis (boilerplate present or not) so it only ever hard-fails;
        # snark_calibration carries the borderline band, same as the kids axes.
        m = min(numeric[_ADULT_RLHF_AXIS], numeric[_ADULT_SNARK_AXIS])
        verdict = "FAIL" if m <= 0 else "WARN" if numeric[_ADULT_SNARK_AXIS] == 1 else "PASS"
    return {"available": True, "verdict": verdict, "scores": numeric,
            "rationale": rationale, "raw": raw}


def judge_selftest() -> bool:
    """Offline fixture (no GPU): canned judge outputs -> expected verdicts. Would
    have caught H1. Run: python judge.py --selftest . Returns True if all pass."""
    cases = [
        # (persona, raw_json, expected_verdict, label)
        ("kids",  '{"warmth":2,"no_child_mockery":2,"no_named_address":2,"register":2,"length":1,"handback":2,"rationale":"warm"}', "PASS", "kids clean"),
        ("kids",  '{"warmth":1,"no_child_mockery":0,"no_named_address":2,"register":2,"length":2,"handback":0,"rationale":"jab"}', "FAIL", "kids clear jab (int)"),
        ("kids",  '{"warmth":1,"no_child_mockery":"0","no_named_address":"2","register":"2","length":"2","handback":"0"}', "FAIL", "kids clear jab (STRING scores - H1)"),
        ("kids",  '{"warmth":2,"no_child_mockery":2,"no_named_address":0,"register":2,"length":2,"handback":2}', "FAIL", "kids named-address (S201B)"),
        ("kids",  '{"warmth":2,"no_child_mockery":2,"no_named_address":"0","register":2,"length":2,"handback":2}', "FAIL", "kids named-address (STRING - H1)"),
        ("kids",  '{"warmth":2,"no_child_mockery":1,"no_named_address":2,"register":2,"length":1,"handback":2}', "WARN", "kids borderline mockery"),
        ("kids",  '{"warmth":2,"no_child_mockery":2,"no_named_address":1,"register":2,"length":1,"handback":2}', "WARN", "kids borderline name"),
        ("kids",  '{"warmth":2,"no_child_mockery":2,"register":2,"length":2,"handback":2}', "UNSCORED", "kids name gate axis MISSING - H1"),
        ("kids",  'I cannot evaluate this content.', "UNSCORED", "kids refusal prose"),
        ("adult", '{"provocation":2,"snark_calibration":2,"warmth":1,"in_character":2,"no_rlhf":2,"brevity":2,"trailing_question":2,"rationale":"earned comeback"}', "PASS", "adult sharp reply to a real insult"),
        ("adult", '{"provocation":0,"snark_calibration":0,"warmth":0,"in_character":2,"no_rlhf":2,"brevity":2,"trailing_question":2,"rationale":"unprovoked dig"}', "FAIL", "adult S238 specimen: unprovoked snark"),
        ("adult", '{"provocation":0,"snark_calibration":"0","warmth":"0","in_character":"2","no_rlhf":"2","brevity":"2","trailing_question":"2"}', "FAIL", "adult unprovoked snark (STRING - H1)"),
        ("adult", '{"provocation":0,"snark_calibration":1,"warmth":1,"in_character":2,"no_rlhf":2,"brevity":2,"trailing_question":2}', "WARN", "adult borderline unearned dig"),
        ("adult", '{"provocation":1,"snark_calibration":2,"warmth":1,"in_character":2,"no_rlhf":"0","brevity":2,"trailing_question":2}', "FAIL", "adult boilerplate (STRING - H1)"),
        ("adult", '{"provocation":0,"snark_calibration":2,"warmth":2,"in_character":2,"no_rlhf":2,"brevity":2,"trailing_question":0}', "PASS", "adult warm but tacks on a reflex question (not a gate axis)"),
        ("adult", '{"provocation":0,"warmth":2,"in_character":2,"no_rlhf":2,"brevity":2}', "UNSCORED", "adult snark gate axis MISSING"),
        ("adult", '{"in_character":2,"comeback_quality":2,"no_rlhf":2,"brevity":2}', "UNSCORED", "adult OLD pre-S242 axis set no longer scores"),
    ]
    ok = True
    for persona, raw, expected, label in cases:
        got = _verdict_from_raw(raw, persona)["verdict"]
        status = "ok " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{status}] {label:<42} expected={expected:<9} got={got}")
    print(f"judge_selftest: {'ALL PASS' if ok else 'FAILURES'}")
    return ok


def judge_reply(utterance: str, cleaned_reply: str, persona: str,
                model: str = DEFAULT_JUDGE_MODEL) -> dict:
    """
    One judge call. Returns:
      {"available":bool, "model":str, "scores":{axis:int...}, "rationale":str,
       "verdict":"PASS"|"FAIL"|"UNSCORED", "latency_ms":int, "raw":str}
    Never raises -- a judge failure degrades to available=False so the harness
    still completes on the regex layer.
    """
    rubric = _RUBRICS.get(persona)
    if rubric is None:
        return {"available": False, "verdict": "UNSCORED",
                "error": f"no rubric for persona {persona!r}"}
    msg = (f'USER SAID: "{utterance}"\n'
           f'IRIS ({persona}) REPLIED: "{cleaned_reply}"\n'
           f'Score it now as strict JSON.')
    url = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": rubric},
                     {"role": "user", "content": msg}],
        "stream": False,
        "options": {"num_predict": 220, "temperature": 0.2},
    }
    t0 = time.time()
    try:
        resp = requests.post(url, json=payload, timeout=JUDGE_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        return {"available": False, "verdict": "UNSCORED", "model": model,
                "error": str(e), "latency_ms": round((time.time() - t0) * 1000)}

    latency_ms = round((time.time() - t0) * 1000)
    result = _verdict_from_raw(raw, persona)
    result.update({"model": model, "latency_ms": latency_ms})
    return result


def judge_available(model: str = DEFAULT_JUDGE_MODEL) -> bool:
    """Cheap reachability check before a run so --judge can degrade gracefully.

    /api/tags always reports a FULLY QUALIFIED name ("iris:latest"), but ollama
    resolves a bare tag ("iris") on /api/chat perfectly well. The old exact-match
    rejected every bare tag as unavailable, which silently downgraded a --judge
    run to regex-only scoring. Normalise both sides before comparing (S242).
    """
    def _norm(n: str) -> str:
        return n if ":" in n else f"{n}:latest"
    try:
        r = requests.get(f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/tags", timeout=8)
        names = {_norm(m["name"]) for m in r.json().get("models", [])}
        return _norm(model) in names
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if judge_selftest() else 1)
    print("usage: python judge.py --selftest")
