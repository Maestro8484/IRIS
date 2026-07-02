#!/usr/bin/env python3
"""
iris_bench_report.py - Parse [BENCH] log lines and print pipeline timing summary.

Usage:
  sudo journalctl -u assistant -n 300 | python3 iris_bench_report.py
  cat /home/pi/logs/assistant.log | python3 iris_bench_report.py

Columns: rec=recording, stt=Whisper, ttfc=time-to-first-chunk, llm=full stream,
         tts=synthesis, aud=playback, TOTAL=wake→audio-done
"""

import sys
import re
import datetime


def parse_bench(line):
    m = re.search(r'\[BENCH\](.*)', line)
    if not m:
        return None
    kv = {}
    for part in m.group(1).split():
        if '=' in part:
            k, v = part.split('=', 1)
            kv[k] = v.strip('"\'')
    return kv if kv else None


def f(val, digits=2):
    try:
        return f"{float(val):.{digits}f}s"
    except (TypeError, ValueError):
        return '-'


def main():
    cycles = []
    cur = {}

    for line in sys.stdin:
        kv = parse_bench(line)
        if not kv:
            continue
        stage = kv.get('stage')
        if not stage:
            continue

        if stage == 'wake_detected':
            if cur:
                cycles.append(cur)
            cur = {'trigger': kv.get('trigger', '?'), '_t': kv.get('t', '')}
        elif cur:
            cur[stage] = kv

    if cur:
        cycles.append(cur)

    if not cycles:
        print("No [BENCH] cycles found.")
        print("Ensure assistant.py + llm.py are the patched versions and the service is running.")
        return

    print(f"\nIRIS Pipeline Benchmark  —  {len(cycles)} cycle(s)\n")
    cols = f"{'#':>3}  {'time':>8}  {'trig':>4}  {'tier':>6}  {'np':>4}  " \
           f"{'rec':>5}  {'stt':>5}  {'ttfc':>5}  {'llm':>5}  {'tts':>5}  {'aud':>5}  " \
           f"{'TOTAL':>6}  {'eval/prompt':>11}  transcript"
    print(cols)
    print('-' * len(cols))

    for i, c in enumerate(cycles, 1):
        ts = c.get('_t', '')
        try:
            ts = datetime.datetime.fromtimestamp(float(ts)).strftime('%H:%M:%S')
        except Exception:
            ts = ts[:8]

        trigger  = c.get('trigger', '?')[:4]
        ls       = c.get('llm_start', {})
        tier     = ls.get('tier', '-')
        np_val   = ls.get('num_predict', '-')

        rec      = c.get('rec_done', {}).get('dur_rec')
        stt      = c.get('stt_done', {}).get('dur_stt')
        ttfc     = c.get('llm_first_chunk', {}).get('dur_ttfc')
        llm      = c.get('llm_done', {}).get('dur_llm')
        tts      = c.get('tts_done', {}).get('dur_tts')
        aud      = c.get('audio_done', {}).get('dur_audio')
        total    = c.get('audio_done', {}).get('dur_total')

        os_d     = c.get('ollama_stats', {})
        e_tok    = os_d.get('eval_tokens', '-')
        p_tok    = os_d.get('prompt_tokens', '-')
        ep       = f"{e_tok}/{p_tok}"

        snip     = c.get('stt_done', {}).get('transcript', '')[:22]

        print(f"{i:>3}  {ts:>8}  {trigger:>4}  {tier:>6}  {np_val:>4}  "
              f"{f(rec):>5}  {f(stt):>5}  {f(ttfc):>5}  {f(llm):>5}  "
              f"{f(tts):>5}  {f(aud):>5}  {f(total):>6}  "
              f"{ep:>11}  \"{snip}\"")

    print()

    # Active tuning levers
    try:
        sys.path.insert(0, '/home/pi')
        from core.config import (
            NUM_PREDICT_SHORT, NUM_PREDICT_MEDIUM, NUM_PREDICT_LONG, NUM_PREDICT_MAX,
            TTS_MAX_CHARS, CHATTERBOX_ENABLED,
        )
        tts_mode = "chatterbox" if CHATTERBOX_ENABLED else "piper"
        print("Active tuning levers (live from core/config + iris_config.json):")
        print(f"  SHORT={NUM_PREDICT_SHORT}  MEDIUM={NUM_PREDICT_MEDIUM}  "
              f"LONG={NUM_PREDICT_LONG}  MAX={NUM_PREDICT_MAX}  "
              f"TTS_MAX_CHARS={TTS_MAX_CHARS}  TTS={tts_mode}")
    except Exception as e:
        print(f"(Could not load config: {e})")

    print()


if __name__ == '__main__':
    main()
