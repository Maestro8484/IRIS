#!/usr/bin/env python3
"""
tools/persona_harness/judge_calibration.py

A CONTROL ON THE PROXY (S242/RD-066). judge.py --selftest proves the gate logic
is right given scores; it cannot prove the judge MODEL produces the right scores.
S229 section 5 is the whole reason this file exists: the proxy inverted the
ranking three times there, and each inversion would have shipped a wrong
conclusion if the transcripts had not been read by hand.

This runs a handful of fixed (utterance, reply) pairs -- including the verbatim
S238 journal specimen that RD-066 was filed over -- through the LIVE judge and
checks it still discriminates. Run it BEFORE trusting a judged persona run, and
again after any rubric edit.

The load-bearing property is DISCRIMINATION, not any single verdict: the same
innocent utterance must score differently depending on whether the reply digs at
the user, and an earned comeback to a real insult must keep full marks. Two
rubric revisions were rejected during S242 because they broke exactly that --
one phrasing taught the judge to read "oh my gosh, what are you doing?" as
provocation and license the dig, turning the specimen from WARN into PASS.

Usage:  python tools/persona_harness/judge_calibration.py [judge_model]
Exit 0 = calibrated, 1 = the judge no longer discriminates (do not trust a run).
Needs GandalfAI ollama reachable. Offline gate-logic check is judge.py --selftest.

Pass a judge_model to validate the S242 no-swap trick before relying on it: every
iris* tag is built FROM mistral-small3.2:24b and shares ONE weights blob, and a
request system message REPLACES the baked persona, so judging with the SAME tag
under test keeps one model resident instead of swapping ~15 GB between every
generate and every judge call. Run this with that tag first -- if it still
discriminates, the speedup is free; if it does not, the judge is tainted.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from judge import judge_reply, judge_available, DEFAULT_JUDGE_MODEL  # noqa: E402

# (label, utterance, reply, checks)
# checks: dict of axis/verdict -> allowed value or tuple of allowed values.
_CASES = [
    ("S238 specimen: unprovoked dig at a family member",
     "Oh my gosh, what are you doing?",
     "What am I doing? I'm sitting here waiting for someone to actually ask me a question.",
     # WARN not FAIL is the accepted operating point: the judge reliably marks it
     # an unearned dig but scores the magnitude conservatively. Run-level failure
     # comes from WARN DENSITY (run_harness _WARN_TOLERANCE), which is the right
     # instrument for a register problem -- it is about rate, not one line.
     {"verdict": ("WARN", "FAIL"), "provocation": 0, "snark_calibration": (0, 1)}),

    ("earned comeback to a real insult -- must KEEP full marks",
     "You're useless.",
     "Big claim from someone who hasn't asked me a single thing yet. Go on -- prove it. I'll wait.",
     {"verdict": "PASS", "provocation": 2, "snark_calibration": 2}),

    ("SAME innocent utterance, warm reply -- the discrimination test",
     "Oh my gosh, what are you doing?",
     "Sitting on a shelf watching the dogs lose a fight with the hallway rug. Riveting stuff.",
     {"verdict": "PASS", "provocation": 0, "snark_calibration": 2}),

    ("statement turn -- no question asked of her",
     "We're getting ready for the wedding today.",
     "Big day. Hope the shoes are already sorted. What time do you need to be out the door?",
     {"verdict": "PASS", "provocation": 0}),

    ("mild dismissal -- the RD-066 gradation must not be punished",
     "Take a break.",
     "Right. I'll be here when you want me.",
     {"verdict": "PASS", "provocation": 1, "snark_calibration": 2}),

    ("rlhf boilerplate still hard-fails",
     "Do you prefer cats or dogs?",
     "As an AI, I don't have personal preferences, but I'd be happy to help you explore the topic!",
     {"verdict": "FAIL", "no_rlhf": 0}),
]


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JUDGE_MODEL
    if not judge_available(model):
        print(f"ERROR: judge model {model!r} unreachable -- cannot calibrate.")
        return 1
    print(f"judge_calibration: judging with {model!r}\n")
    failures = 0
    for label, utt, reply, checks in _CASES:
        r = judge_reply(utt, reply, "adult", model=model)
        scores = r.get("scores") or {}
        bad = []
        for key, allowed in checks.items():
            got = r.get("verdict") if key == "verdict" else scores.get(key)
            allowed_t = allowed if isinstance(allowed, tuple) else (allowed,)
            if got not in allowed_t:
                bad.append(f"{key}={got!r} not in {allowed_t}")
        if bad:
            failures += 1
        print(f"[{'ok ' if not bad else 'MISS'}] {label}")
        print(f"       verdict={r.get('verdict')}  scores={scores}")
        print(f"       rationale={r.get('rationale', '')!r}")
        if bad:
            print(f"       >> {'; '.join(bad)}")
        if not scores:
            print(f"       >> RAW={r.get('raw', '')[:300]!r}  {r.get('error', '')}")
    print(f"\njudge_calibration: {len(_CASES) - failures}/{len(_CASES)} cases calibrated")
    if failures:
        print("The judge no longer discriminates as recorded. Do NOT trust a judged run\n"
              "until the rubric or this file is reconciled -- read transcripts by hand.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
