"""
core/prompt.py -- the ONE message-composition pipeline (RD-064).

Both the spoken path (assistant._build_messages) and the typed WebUI path
(iris_web.api_chat) build their Ollama /api/chat messages list HERE, so a
question typed into the WebUI is composed exactly like the same question spoken:
the same date stamp, the same kids notebook, the same personality-tuning clause,
the same episodic-recall lookup and miss clause, the same conversation history,
and the same S226 token-budget trim.

Before RD-064 the WebUI hand-rolled a date+tuning-only /api/generate prompt with
no history and no recall, so a memory-shaped question TYPED into the WebUI made
her invent (the RD-060 / S224d confabulation class) while the very same question
SPOKEN went through recall and stayed honest. Typed testing was therefore not a
faithful proxy for the voice, and the prompt builder was duplicated in two
places that drifted. This module removes both problems.

LOAD-BEARING INVARIANTS -- do not "tidy" these away:

  * NEVER emit a {"role":"system"} message. In Ollama a request system message
    REPLACES the modelfile SYSTEM persona and strips it, turning IRIS into a
    generic apologetic assistant (proven S134). Every context fragment is folded
    into the CURRENT user turn instead, which the persona is told to expect
    ("Date and time context may be provided to you").

  * At neutral tuning (all sliders 0.5), recall flag off, adult non-game turn,
    the composed prompt is BYTE-IDENTICAL to pre-RD-064 IRIS. `--selftest` proves
    build_messages() matches a frozen copy of the old assistant._build_messages()
    assembly over a matrix of histories/modes AND at the S226 budget boundary. If
    it ever differs, STOP -- something in the fold order, the date format, or the
    trim changed.

  * The S226 token-budget trim (_fit_context_budget) runs LAST, so the stamp and
    every clause sit INSIDE the budget. Dropping it re-opens the RD-052/S226
    truncation cliff where a full history starved the reply and she stopped
    mid-sentence.

FRAGMENT ORDER IS THE CONTRACT. The clause closest to the user's words wins, so
the strongest steer sits last: date, then kids notebook, then tuning, then
trajectory, then recall. This is the order the voice path shipped; the WebUI now
matches it exactly.

WHERE CONFIG COMES FROM (and why there is no `cfg` argument). Tuning and the
budget knobs are read live from core.config, exactly as the voice path always
has, so the spoken prompt is byte-identical by construction rather than by a
caller passing a matching dict. The web process keeps core.config fresh by
calling core.config.reload_overrides() before it composes, which is the same
mechanism the assistant uses on RELOAD_CONFIG -- so a just-saved slider or a
RECALL_* flag is live for typed chat without an iris-web restart.

Run `python core/prompt.py --selftest` (no GPU, no network) to exercise it.
"""
from __future__ import annotations

import datetime


# ── Token budget (S226) -- moved verbatim from assistant.py at RD-064 ──────────
# These two were private to assistant._build_messages and moved here unchanged so
# the trim lives with the only thing that calls it. The estimate is deliberately
# biased HIGH: trimming one exchange early costs a little context, trimming one
# late costs her the end of a sentence.

def _est_tokens(text: str) -> int:
    """S226: cheap per-message token estimate, deliberately biased HIGH.

    Measured against the live tokenizer on real IRIS text (her replies, kids
    questions, household utterances): 4.36 characters per token pooled, 3.64 on
    the tightest sample. Dividing by 3.6 over-counts by roughly a fifth, which
    is the direction we want -- trimming one exchange early costs a bit of
    context, trimming one exchange late costs her the end of a sentence.

    The +4 is the chat template's per-message wrapper, measured at 5 tokens per
    exchange (see docs/S226_context_budget_findings.md).
    """
    return int(len(text) / 3.6) + 4


def _fit_context_budget(msgs: list, num_predict: int) -> list:
    """S226: bound the prompt by TOKEN budget, leaving room to actually reply.

    Ollama trims the oldest messages until prompt <= num_ctx and then hands
    whatever is left to generation. Nothing reserves room to speak, so a full
    history simply starves the reply: on 2026-07-22 prompt_tokens hit 6141 of
    6144 and she said "Night." and stopped. Reproduced at will -- a ten-exchange
    history of 224-token replies is a 6,380-token prompt, truncated to 6,136
    with eval_tokens=8.

    The 20-message cap in the append sites cannot see this, because a turn is
    not a fixed number of tokens: at NUM_PREDICT_MEDIUM ten exchanges cost about
    2,490 tokens on their own.

    Operates on the COPY built by build_messages(). state.conversation_history
    is never touched, so nothing is lost from the log, from recall, or from the
    story-resume window.
    """
    import core.config as _cfg
    budget = (_cfg.LLM_NUM_CTX - _cfg.LLM_PERSONA_TOKENS
              - max(int(num_predict or 0), 0) - _cfg.LLM_CTX_SAFETY_TOKENS)
    if budget <= 0 or not msgs:
        return msgs
    total = sum(_est_tokens(m.get("content", "")) for m in msgs)
    if total <= budget:
        return msgs
    dropped = 0
    # Drop whole exchanges from the oldest end. The final user turn always
    # survives: it carries the stamp, the clauses, and the actual question.
    while len(msgs) > 2 and total > budget:
        for _ in range(2):
            if len(msgs) <= 1:
                break
            total -= _est_tokens(msgs[0].get("content", ""))
            msgs.pop(0); dropped += 1
    if not dropped:
        # Nothing droppable: a single turn whose own stamp and clauses already
        # exceed the budget. Irreducible here, and the safety margin plus
        # Ollama's own prompt trim absorb it. Say so rather than pretending.
        print(f"[CTX]  over token budget {budget} by ~{total - budget} on a single "
              f"turn -- nothing to drop", flush=True)
        return msgs
    if not (msgs and msgs[0].get("content", "").startswith("[Earlier conversation")):
        msgs.insert(0, {"role": "assistant", "content": "[Earlier conversation omitted]"})
    # RD-031: bounded by construction -- at most one line per turn, and only on
    # a turn that actually trimmed. Silence here is what made this invisible.
    print(f"[CTX]  token budget {budget} (num_predict {num_predict}): dropped "
          f"{dropped} oldest message(s), ~{total} tokens of history sent", flush=True)
    return msgs


# ── The one composer both services call ───────────────────────────────────────

def build_messages(text, history, mode, recall_clause="", *,
                   weather_clause="", trajectory_clause="", cam_game_active=False,
                   now=None, num_predict=None) -> list:
    """Compose the Ollama /api/chat messages list for one turn.

    text     the current user utterance (raw), or None to fold the stamp onto the
             last user turn already present in `history` (the voice path, whose
             turn is appended to state.conversation_history before it composes).
    history  prior turns as a messages list [{role, content}, ...]; may be empty.
             Copied, never mutated. For the voice path this is the full history
             (with the current turn last) and text is None; for the typed path it
             is the client transcript (prior turns) and text is the new question.
    mode     "kids" or anything else (adult).
    weather_clause     RD-068: the real outside conditions, when the utterance was
             one they bear on ("" otherwise, and "" whenever the fetch failed --
             services.weather fails closed so she is never handed a stale or
             guessed number). Supplied by the caller for the same reason recall is:
             the voice path gets it from the router's payload, the typed path calls
             services.weather.clause_for directly.
    recall_clause      the episodic-recall clause the caller looked up via
             core.recall.prepare(text) -- "" when recall is off, the utterance is
             not a memory question, nothing cleared the threshold, or the caller
             is mid camera-game. Passed in because the lookup is caller state
             (the voice path refreshes it in _route_predict; the typed path calls
             recall.prepare directly).
    trajectory_clause  the dark S221-Phase-2 steering clause (voice only; ""
             everywhere else). Gated to adult, non-game here so the gate lives in
             one place.
    cam_game_active    suppresses trajectory AND recall, exactly as the voice
             path does during a camera game (a game follow-up can reach here
             without re-routing, so a stale clause could leak in).
    now      injectable datetime for a deterministic selftest; defaults to real
             wall-clock.
    num_predict        the tier's reply-token budget, fed to the S226 trim so the
             history left in the prompt leaves room to actually reply.

    Returns the messages list ready for /api/chat. NEVER contains a role:system
    message (S134): every fragment is folded into the current user turn.
    """
    kids_mode = (mode == "kids")
    if now is None:
        now = datetime.datetime.now()

    # Work on a COPY so the caller's stored history keeps the raw user text (no
    # stamp, no clauses) for the log, recall, and the story-resume window.
    msgs = [dict(m) for m in (history or [])]
    if text is not None:
        msgs.append({"role": "user", "content": text})

    # The date the persona is told to expect. Folded into the user turn, NEVER a
    # role:system message (S134). This exact format is the byte-identical anchor.
    stamp = f"(Context, not spoken: it is {now.strftime('%A, %B %d %Y, %I:%M %p')} Mountain Time.) "

    # 0. Weather (RD-068), immediately after the date because it is the same KIND
    #    of thing: plain world state, not a steer. The order contract puts the
    #    strongest steering closest to the question, and a temperature steers
    #    nothing -- it is a fact she may or may not need. Empty on every turn the
    #    weather does not bear on, so the stamp stays byte-identical there.
    if weather_clause:
        stamp = stamp + weather_clause + " "

    # 1. Kids notebook (kids mode only): who the children are + what they have
    #    been on about lately. The literal "little notebook Dad keeps" (RD-047).
    if kids_mode:
        try:
            from core import kids_profile
            _prof = kids_profile.prompt_context()
            if _prof:
                stamp = stamp + _prof + " "
        except Exception as _pe:
            print(f"[KIDPROF] context injection skipped: {_pe}", flush=True)

    # 2. Personality-continuum tuning (S215). Reads the live slider values from
    #    core.config (reload_overrides keeps them current). Neutral -> "" ->
    #    byte-identical to pre-continuum IRIS.
    try:
        from core import persona_tuning
        _tun = persona_tuning.tuning_clause(kids_mode)
        if _tun:
            stamp = stamp + _tun + " "
    except Exception as _te:
        print(f"[TUNE] context injection skipped: {_te}", flush=True)

    # 3. Trajectory steering (S221 Phase 2, dark). Adult, non-game only.
    if trajectory_clause and not kids_mode and not cam_game_active:
        stamp = stamp + trajectory_clause + " "

    # 4. Episodic recall (S224c), LAST so the remembered material sits closest to
    #    the question that asked for it. Suppressed mid camera-game.
    if recall_clause and not cam_game_active:
        stamp = stamp + recall_clause + " "

    # Fold the whole stamp onto the LAST user turn -- the one carrying the actual
    # question. (When text was appended above, that is the appended turn.)
    for m in reversed(msgs):
        if m.get("role") == "user":
            m["content"] = stamp + m["content"]
            break

    # S226 LAST, so the stamp and every clause are inside the budget.
    return _fit_context_budget(msgs, num_predict)


# ── selftest: offline, no GPU, no network ─────────────────────────────────────

def _ref_build(history, kids_mode, traj_clause, recall_clause, cam_game,
               num_predict, now, text=None, weather_clause=""):
    """FROZEN copy of the pre-RD-064 assistant._build_messages() assembly.

    build_messages() must match this byte-for-byte over the fixtures below. This
    is the whole point of the guard: if a future edit reorders the clauses,
    changes the date format, or drops the budget trim, the mismatch is caught
    offline before it can reach the model that produces every spoken reply.

    RD-068 added the weather fragment here as well as in build_messages, because
    a reference that omits a real fragment can only ever prove the fragment is
    absent. What keeps this honest is that equality is not the only check: the
    hardcoded position assertions below state the expected string outright, so
    the weather clause has to land in one exact place or the selftest fails.
    """
    msgs = [dict(m) for m in (history or [])]
    if text is not None:
        msgs.append({"role": "user", "content": text})
    stamp = f"(Context, not spoken: it is {now.strftime('%A, %B %d %Y, %I:%M %p')} Mountain Time.) "
    if weather_clause:
        stamp = stamp + weather_clause + " "
    if kids_mode:
        try:
            from core import kids_profile
            _prof = kids_profile.prompt_context()
            if _prof:
                stamp = stamp + _prof + " "
        except Exception:
            pass
    try:
        from core import persona_tuning
        _tun = persona_tuning.tuning_clause(kids_mode)
        if _tun:
            stamp = stamp + _tun + " "
    except Exception:
        pass
    if traj_clause and not kids_mode and not cam_game:
        stamp = stamp + traj_clause + " "
    if recall_clause and not cam_game:
        stamp = stamp + recall_clause + " "
    for m in reversed(msgs):
        if m.get("role") == "user":
            m["content"] = stamp + m["content"]
            break
    return _fit_context_budget(msgs, num_predict)


def _selftest() -> int:
    ran, fails = [], []

    def check(label, cond):
        ran.append(label)
        print("%-62s %s" % (label, "PASS" if cond else "FAIL"))
        if not cond:
            fails.append(label)

    # Deterministic clock and controlled clause sources so the guard depends on
    # NOTHING live -- no config, no /home/pi files, no GandalfAI. Both the frozen
    # reference and build_messages read through these same patched functions, so
    # the equality proves the ASSEMBLY, not the clause contents.
    now = datetime.datetime(2026, 7, 23, 14, 5, 0)
    import core.persona_tuning as _pt
    import core.kids_profile as _kp
    _orig_tun, _orig_prof = _pt.tuning_clause, _kp.prompt_context
    _pt.tuning_clause = lambda kids: ("(Context, not spoken: TUNE-KIDS.)" if kids
                                      else "(Context, not spoken: TUNE-ADULT.)")
    _kp.prompt_context = lambda *a, **k: "(Context, not spoken: KIDS-NOTEBOOK.)"

    U = lambda s: {"role": "user", "content": s}
    A = lambda s: {"role": "assistant", "content": s}

    def _eq(label, **kw):
        """build_messages(...) must equal the frozen reference for these args."""
        text = kw.pop("text", None)
        got = build_messages(
            text, kw["history"], ("kids" if kw["kids"] else "adult"),
            recall_clause=kw.get("recall", ""),
            weather_clause=kw.get("wx", ""),
            trajectory_clause=kw.get("traj", ""),
            cam_game_active=kw.get("cam", False),
            now=now, num_predict=kw.get("np", 224))
        ref = _ref_build(kw["history"], kw["kids"], kw.get("traj", ""),
                         kw.get("recall", ""), kw.get("cam", False),
                         kw.get("np", 224), now, text=text,
                         weather_clause=kw.get("wx", ""))
        check(label, got == ref)
        return got

    try:
        DATE = f"(Context, not spoken: it is {now.strftime('%A, %B %d %Y, %I:%M %p')} Mountain Time.) "

        # 1. VOICE shape (text=None, current turn already last in history).
        h = [U("hello"), A("Hi there."), U("what's the capital of France")]
        g = _eq("voice: adult, no clauses", history=h, kids=False)
        check("voice: stamp folded onto last user turn only",
              g[-1]["content"] == DATE + "(Context, not spoken: TUNE-ADULT.) "
              + "what's the capital of France"
              and g[0]["content"] == "hello" and g[1]["content"] == "Hi there.")
        check("voice: no role:system anywhere (S134)",
              all(m["role"] != "system" for m in g))

        # 2. TYPED/WebUI shape (text supplied, history = prior only). Appending
        #    the current turn must land at the SAME place the voice path has it.
        g2 = _eq("typed: adult, appends current turn",
                 history=[U("hello"), A("Hi there.")], text="what's the capital of France",
                 kids=False)
        check("typed shape == voice shape for the same conversation",
              g2 == g)

        # 3. Recall clause folds LAST and only into the current user turn.
        rc = "(Context, not spoken: you checked your record and found nothing...)"
        g3 = _eq("recall clause present (adult)", history=h, kids=False, recall=rc)
        check("recall sits at the very end of the stamp, before the question",
              g3[-1]["content"].endswith(rc + " " + "what's the capital of France"))
        check("recall clause never touches an earlier turn",
              rc not in g3[0]["content"] and rc not in g3[1]["content"])

        # 4. Kids mode folds the notebook + the kids tuning fragment.
        gk = _eq("kids: notebook + kids tuning", history=[U("tell me a joke")], kids=True)
        check("kids notebook and kids tuning both fold in, in order",
              gk[-1]["content"] == DATE + "(Context, not spoken: KIDS-NOTEBOOK.) "
              + "(Context, not spoken: TUNE-KIDS.) " + "tell me a joke")

        # 5. Camera game suppresses BOTH trajectory and recall.
        gc = _eq("cam game suppresses traj+recall", history=h, kids=False,
                 recall=rc, traj="(Context, not spoken: TRAJ.)", cam=True)
        check("cam-game turn carries neither recall nor trajectory",
              rc not in gc[-1]["content"] and "TRAJ" not in gc[-1]["content"])

        # 6. Trajectory folds for adult non-game, between tuning and recall.
        gt = _eq("trajectory folds (adult, non-game)", history=h, kids=False,
                 traj="(Context, not spoken: TRAJ.)", recall=rc)
        check("order is date, tuning, trajectory, recall",
              gt[-1]["content"] == DATE + "(Context, not spoken: TUNE-ADULT.) "
              + "(Context, not spoken: TRAJ.) " + rc + " "
              + "what's the capital of France")
        check("trajectory is dropped in kids mode",
              "TRAJ" not in _eq("trajectory dropped in kids", history=[U("hi")],
                                kids=True, traj="(Context, not spoken: TRAJ.)")[-1]["content"])

        # 6b. WEATHER (RD-068) sits immediately after the date and before every
        #     steering clause, in both shapes, and never touches an earlier turn.
        WX = "(Context, not spoken: the current weather outside is 68 degrees Fahrenheit and overcast.)"
        gw = _eq("weather folds (adult)", history=h, kids=False, wx=WX)
        check("weather sits directly after the date, before tuning",
              gw[-1]["content"] == DATE + WX + " "
              + "(Context, not spoken: TUNE-ADULT.) " + "what's the capital of France")
        check("weather never touches an earlier turn",
              WX not in gw[0]["content"] and WX not in gw[1]["content"])
        gwk = _eq("weather folds (kids, before the notebook)",
                  history=[U("is it cold out")], kids=True, wx=WX)
        check("kids order is date, weather, notebook, tuning",
              gwk[-1]["content"] == DATE + WX + " "
              + "(Context, not spoken: KIDS-NOTEBOOK.) "
              + "(Context, not spoken: TUNE-KIDS.) " + "is it cold out")
        gwa = _eq("weather coexists with trajectory and recall", history=h, kids=False,
                  wx=WX, traj="(Context, not spoken: TRAJ.)", recall=rc)
        check("full order is date, weather, tuning, trajectory, recall",
              gwa[-1]["content"] == DATE + WX + " "
              + "(Context, not spoken: TUNE-ADULT.) "
              + "(Context, not spoken: TRAJ.) " + rc + " "
              + "what's the capital of France")
        check("no weather clause leaves the stamp byte-identical",
              _eq("weather empty == no weather arg", history=h, kids=False, wx="")
              == build_messages(None, h, "adult", now=now, num_predict=224))
        gwt = _eq("typed path folds weather the same way", history=[U("hello"), A("Hi.")],
                  text="is it cold out", kids=False, wx=WX)
        check("typed weather turn matches the voice assembly",
              gwt[-1]["content"] == DATE + WX + " "
              + "(Context, not spoken: TUNE-ADULT.) " + "is it cold out")

        # 7. Fold targets the last USER message even when history ends assistant.
        h_end_a = [U("first"), A("reply one"), U("second"), A("reply two")]
        ge = _eq("fold targets last user, not last message", history=h_end_a, kids=False)
        check("stamp landed on 'second', not on the trailing assistant turn",
              ge[2]["content"].endswith("second") and ge[2]["content"].startswith(DATE)
              and ge[3]["content"] == "reply two")

        # 8. THE BUDGET BOUNDARY (S226). A history over budget must be trimmed,
        #    the marker inserted, and the current turn (with its full stamp) kept.
        big = []
        for i in range(40):
            big.append(U(("q%02d " % i) + "x" * 800))
            big.append(A(("a%02d " % i) + "y" * 800))
        big.append(U("do you remember what I told you"))
        gb = _eq("budget boundary: over-budget history is trimmed",
                 history=big, kids=False, recall=rc, np=224)
        check("budget: result has FEWER messages than the input",
              len(gb) < len(big))
        check("budget: '[Earlier conversation omitted]' marker inserted",
              gb[0]["content"].startswith("[Earlier conversation"))
        check("budget: the current turn survives with its full stamp + recall",
              gb[-1]["content"].startswith(DATE)
              and gb[-1]["content"].endswith(rc + " do you remember what I told you"))
        # And under budget, nothing is dropped.
        gu = _eq("under budget: nothing trimmed", history=h, kids=False, np=224)
        check("under budget: message count unchanged", len(gu) == len(h))

        # 9. history is never mutated (the caller's stored turns stay raw).
        h_src = [U("keep me raw")]
        build_messages("new turn", h_src, "adult", now=now, num_predict=224)
        check("caller history is not mutated",
              h_src == [{"role": "user", "content": "keep me raw"}])

        # 10. Empty history + text (a first typed turn) composes cleanly.
        g0 = _eq("empty history + first typed turn", history=[], text="hello there",
                 kids=False)
        check("first turn carries the stamp and the text",
              len(g0) == 1 and g0[0]["role"] == "user"
              and g0[0]["content"] == DATE + "(Context, not spoken: TUNE-ADULT.) "
              + "hello there")
    finally:
        _pt.tuning_clause, _kp.prompt_context = _orig_tun, _orig_prof

    print("\n%d/%d PASS" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys, os
    # Make `core` importable whether run as `python core/prompt.py` or `-m core.prompt`
    # (same hop persona_tuning's selftest uses).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python core/prompt.py --selftest")
