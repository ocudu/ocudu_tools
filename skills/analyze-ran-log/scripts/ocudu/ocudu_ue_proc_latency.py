#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Per-stage latency profiler for OCUDU DU UE-lifecycle procedures.

Measures DU-MNG / MAC / SCHED / DU-F1 procedure stages for UE creation,
(re)configuration and removal, with parsers.log.procedures of this repo.
Stages that a build does not log are skipped (reports only present stages). At info level, the MAC and scheduler
procedures log only their done stage, so they have no latency; UE Create, UE Configuration and UE Delete have theirs.

Usage: ocudu_ue_proc_latency.py <gnb.log|srsdu.log|run dir> [proc-filter-substr]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))

from parsers.log import procedures, run  # noqa: E402


def fmt(us: float) -> str:
    return f"{us / 1000:.2f}ms" if us >= 1000 else f"{us:.0f}us"


def report(proc: str, insts: list[procedures.Instance]) -> None:
    print(f'\n### proc="{proc}"   instances={len(insts)}')
    rows = []
    for label, frm, to in procedures.transitions(proc):
        st = procedures.stats(procedures.latencies(insts, frm, to))
        # A stage that the build does not log.
        if st is not None:
            rows.append(f"{label:34} {st.n:>5} {fmt(st.mean):>9} {fmt(st.p50):>9} {fmt(st.p90):>9} {fmt(st.p99):>9} "
                        f"{fmt(st.max):>10}")
    if not rows:
        logged = sorted({stage for inst in insts for stage in inst.stages})
        print(f"no latency: only stage {', '.join(logged)} logged; the others are likely at debug level, e.g. the "
              f"MAC and scheduler starts need mac_level=debug")
        return
    hdr = f"{'stage transition':34} {'n':>5} {'mean':>9} {'p50':>9} {'p90':>9} {'p99':>9} {'max':>10}"
    print(hdr)
    print("-" * len(hdr))
    print("\n".join(rows))


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-stage latencies of the DU UE procedures of an OCUDU log.")
    ap.add_argument("path", help="OCUDU log, or run directory holding one")
    ap.add_argument("filter", nargs="?", help="keep the procedures whose name contains this text")
    args = ap.parse_args()
    try:
        path = run.find_log(args.path)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    filt = args.filter
    with open(path, errors="replace") as f:
        found = procedures.instances(f)
    print(f"# log: {path}")
    for group, procs in procedures.GROUPS:
        procs = [p for p in procs if found[p] and (not filt or filt in p)]
        if not procs:
            continue
        print(f"\n========== {group} ==========")
        for p in procs:
            report(p, found[p])
    return 0


if __name__ == "__main__":
    sys.exit(main())
