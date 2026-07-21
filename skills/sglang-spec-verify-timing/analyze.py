#!/usr/bin/env python3
"""Measure per-step timing of a named annotation on the main compute stream.

Pools all given trace files into ONE group. Call once per group.

Usage:
  analyze.py --step-name "step[TARGET_VERIFY bs=16]" [--trim 1] FILE [FILE ...]

Each FILE may be a .trace.json or .trace.json.gz (chrome/kineto trace).
Prints: detected main stream per file, and pooled group avg duration & gap (ms).
"""
import argparse, gzip, json, os, statistics
from collections import defaultdict


def load_events(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as f:
        return json.load(f)["traceEvents"]


def main_stream_rows(events, step_name):
    """Return verify events on the main compute stream (max-median-dur GPU stream)."""
    rows = [e for e in events
            if e.get("name") == step_name and e.get("ph") == "X"
            and e.get("cat") == "gpu_user_annotation"]
    if not rows:
        return None, []
    by_tid = defaultdict(list)
    for e in rows:
        by_tid[e["tid"]].append(e)
    main_tid = max(by_tid, key=lambda t: statistics.median(x["dur"] for x in by_tid[t]))
    return main_tid, sorted(by_tid[main_tid], key=lambda e: e["ts"])


def file_samples(path, step_name, trim):
    tid, srows = main_stream_rows(load_events(path), step_name)
    if not srows:
        return tid, [], []
    if trim and len(srows) > 2 * trim:
        srows = srows[trim:-trim]                       # drop warmup/cooldown edges
    durs = [e["dur"] for e in srows]
    gaps = [srows[i + 1]["ts"] - (srows[i]["ts"] + srows[i]["dur"])
            for i in range(len(srows) - 1)]             # end -> next start
    medd = statistics.median(durs)
    durs = [x for x in durs if x <= 5 * medd]           # drop window-break outliers
    if gaps:
        medg = statistics.median(gaps)
        gaps = [x for x in gaps if x <= 5 * medg]
    return tid, durs, gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step-name", required=True)
    ap.add_argument("--trim", type=int, default=1, help="events dropped from each end per file")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()

    durs, gaps = [], []
    for path in a.files:
        tid, d, g = file_samples(path, a.step_name, a.trim)
        print(f"  {os.path.basename(path)} -> main stream {tid}, {len(d)} steps")
        durs += d
        gaps += g
    if not durs:
        print(f"NO EVENTS matched {a.step_name!r}")
        return
    print(f"step={a.step_name}  n={len(durs)}")
    print(f"  avg duration = {statistics.mean(durs) / 1000:.3f} ms")
    print(f"  avg gap      = {statistics.mean(gaps) / 1000:.3f} ms" if gaps else "  avg gap = n/a")


if __name__ == "__main__":
    main()
