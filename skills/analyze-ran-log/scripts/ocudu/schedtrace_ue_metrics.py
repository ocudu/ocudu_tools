#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
schedtrace_ue_metrics.py — What the scheduler did for each UE, from OCUDU scheduler event traces.

Decodes schedtrace_cell<N>.bin with the `parsers` package of this repo and prints, per UE, the throughput, MCS,
PRBs, retransmissions, HARQ-ACK outcomes, CQI, RI and SRs: a summary over the trace, then one row per period per UE,
or the grants and reports themselves. Field names follow the METRICS "Scheduler UE" lines where they mean the same:
dl_brate is of the ACKed TBS, dl_nok counts NACK and DTX reports, of which dl_dtx the latter, cqi and dl_ri are means
of the reports and dl_mcs of the codewords, cqi and dl_mcs rounded half up as METRICS prints them. The trace has no UL CRC or buffer status, so UL throughput is of the scheduled new transmissions
(ul_sched_brate), and HARQ timeouts without a report are in neither dl_ok nor dl_nok.

Usage:
  python3 schedtrace_ue_metrics.py <path>                              # summary, then 1 s periods per UE
  python3 schedtrace_ue_metrics.py <path> --rnti 0x4601 --period 0.01  # one UE, per radio frame
  python3 schedtrace_ue_metrics.py <path> --grants dl --rnti 0x4601 --after 13:35:40 --before 13:35:41
  python3 schedtrace_ue_metrics.py <path> --summary

<path>: a schedtrace_cell<N>.bin, or a directory, whose schedtrace_cell*.bin are all read.

Options:
  --rnti <R>          Keep this UE, by RNTI in any form: 4601 (hex as logs print it), 0x4601, or 17921 (decimal as
                      F1AP pcaps hold it). Repeatable.
  --ue <N>            Keep this UE, by DU UE index. Repeatable.
  --after <T>         Keep slots at or after T: an ISO time (2026-10-12T13:35:40 or 2026-10-12 13:35:40), a date, or
                      a time of day (13:35:40.5), compared to the same precision, UTC.
  --before <T>        Keep slots at or before T, as --after.
  --period <S>        Seconds per row of the periods, finite and positive (default: 1), from whole multiples of it since the epoch,
                      UTC, unlike METRICS periods, which end at the times of their lines.
  --summary           Print only the summary per UE.
  --grants <K>        Print the grants or reports of kind dl, ul, harq or csi instead of the periods.
  --max-rows <N>      Period, grant or report rows printed at most per trace (default: 200).
  -j <K>              Worker processes (default: one per CPU up to 8 for traces over 16 MB, else 1).
"""

from __future__ import annotations

import argparse
import math
import mmap
import multiprocessing
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))

from parsers import schedtrace  # noqa: E402
from parsers.log.search import iso_bound  # noqa: E402
from parsers.ran.rnti import parse_any  # noqa: E402
from parsers.schedtrace import grid, ue_metrics  # noqa: E402
from parsers.schedtrace.ue_metrics import round_half_up  # noqa: E402

# Traces from this size are read in parallel by default.
PARALLEL_MIN_SIZE = 16 << 20
MAX_DEFAULT_WORKERS = 8
CHUNKS_PER_WORKER = 4
# Kinds of slot items of --grants.
GRANT_KINDS = {"dl": "dl_grants", "ul": "ul_grants", "harq": "harq_acks", "csi": "csi_reports"}


def traces_in(path_str: str) -> list[Path]:
    p = Path(path_str)
    if p.is_file():
        return [p]
    if p.is_dir():
        found = sorted(p.rglob("schedtrace_cell*.bin"))
        if found:
            return found
    raise FileNotFoundError(f"No schedtrace_cell*.bin at {p}")


def time_text(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def in_window(ts: str, after: str | None, before: str | None) -> bool:
    """Whether an ISO time is within the bounds, normalized by iso_bound(): ISO times, dates or times of day, compared
    to their precision.
    """
    def key(ref: str) -> str:
        return ts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref[:10]) else ts[11:]

    return (not after or key(after)[: len(after)] >= after) and (not before or key(before)[: len(before)] <= before)


def read_chunk(path: str, start: int | None, end: int | None, opts: dict) -> dict:
    """The aggregates and kept items of the slots of the events in the byte range [start, end) of a trace."""
    agg = ue_metrics.Aggregator(opts["period"])
    kept: list[tuple] = []
    nof_slots, t_min, t_max, latencies = 0, None, None, []
    rntis, ues = set(opts["rntis"]), set(opts["ues"])
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        cell = grid.cell_of(schedtrace.start_event(mm, path, grid_only=True))
        # From the first event, without checking the version again.
        for offset in schedtrace.offsets(mm, schedtrace.HEADER.size if start is None else start, end):
            event = schedtrace.event_at(mm, offset, grid_only=True)
            if event["type"] != "CellSlotEvent":
                continue
            t = event["timestamp_us"] / 1e6
            ts = time_text(t)
            if not in_window(ts, opts["after"], opts["before"]):
                continue
            items = ue_metrics.slot_items(event, cell)
            if rntis or ues:
                items = {kind: [row for row in rows if (not rntis or row[0] in rntis) and (not ues or row[1] in ues)]
                         for kind, rows in items.items()}
            # Records link rows to slot events in viz; this script prints none.
            agg.add(0, event, items)
            nof_slots += 1
            latencies.append(event.get("latency_us") or 0)
            t_min = t if t_min is None else min(t_min, t)
            t_max = t if t_max is None else max(t_max, t)
            if opts["grants"]:
                kept.extend((ts, *row) for row in items[GRANT_KINDS[opts["grants"]]])
    return {"agg": agg, "kept": kept, "nof_slots": nof_slots, "t_min": t_min, "t_max": t_max,
            "slot_s": cell.slot_duration_s, "latency_sum": sum(latencies), "latency_max": max(latencies, default=0)}


def read_trace(path: Path, workers: int, opts: dict) -> dict:
    if workers <= 1:
        return read_chunk(str(path), None, None, opts)
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        offsets = schedtrace.offsets(mm)
    if not offsets:
        return read_chunk(str(path), None, None, opts)
    step = max(1, -(-len(offsets) // (workers * CHUNKS_PER_WORKER)))
    bounds = [*offsets[::step], path.stat().st_size]
    args = [(str(path), lo, hi, opts) for lo, hi in zip(bounds, bounds[1:])]
    # Workers fork from a single-threaded script, so the default start method is safe and fastest.
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("fork")) as pool:
        parts = list(pool.map(read_chunk, *zip(*args)))
    out = parts[0]
    for part in parts[1:]:
        out["agg"].merge(part["agg"])
        out["kept"].extend(part["kept"])
        out["nof_slots"] += part["nof_slots"]
        out["latency_sum"] += part["latency_sum"]
        out["latency_max"] = max(out["latency_max"], part["latency_max"])
        if part["t_min"] is not None:
            out["t_min"] = part["t_min"] if out["t_min"] is None else min(out["t_min"], part["t_min"])
            out["t_max"] = part["t_max"] if out["t_max"] is None else max(out["t_max"], part["t_max"])
    return out


def num(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def mbps(bps: float | None) -> str:
    return "-" if bps is None else f"{bps / 1e6:.2f}"


def retx_pct(newtx: int, retx: int) -> str:
    return "-" if not newtx + retx else f"{100 * retx / (newtx + retx):.1f}"


def print_summary(res: dict) -> None:
    duration = res["t_max"] - res["t_min"] + res["slot_s"]
    print(f"slots={res['nof_slots']} span={time_text(res['t_min'])} .. {time_text(res['t_max'])} ({duration:.3f} s) "
          f"latency_avg={res['latency_sum'] / res['nof_slots']:.1f}us latency_max={res['latency_max']}us")
    header = ("rnti", "ue", "dl_Mbps", "dl_sched_Mbps", "dl_mcs", "dl_err%", "dl_retx%", "dl_prbs/s",
              "ul_sched_Mbps", "ul_retx%", "ul_prbs/s", "cqi", "dl_ri", "rsrp", "sr")
    rows = []
    for _, _, *values in res["agg"].ue_totals(duration):
        v = dict(zip(ue_metrics.UE_FIELDS, values))
        rows.append((v["rnti"], num(v["ue_index"]), mbps(v["dl_brate"]),
                     mbps(v["dl_sched_brate"]), num(round_half_up(v["dl_mcs"])), num(v["dl_error_rate"]),
                     retx_pct(v["dl_nof_newtx"], v["dl_nof_retx"]), num(v["dl_nof_prbs"] / duration, 0),
                     mbps(v["ul_sched_brate"]), retx_pct(v["ul_nof_newtx"], v["ul_nof_retx"]),
                     num(v["ul_nof_prbs"] / duration, 0), num(round_half_up(v["cqi"])), num(v["dl_ri"]), num(v["rsrp"]),
                     num(v["sr_count"])))
    print_table(header, rows)


def print_periods(res: dict, max_rows: int) -> None:
    header = ("time", "rnti", "dl_Mbps", "dl_sched_Mbps", "dl_mcs", "dl_ok", "dl_nok", "dl_dtx", "dl_retx", "dl_prbs",
              "ul_sched_Mbps", "ul_retx", "ul_prbs", "cqi", "dl_ri", "sr")
    rows = []
    for t, _, *values in res["agg"].ue_rows():
        v = dict(zip(ue_metrics.UE_FIELDS, values))
        rows.append((time_text(t)[11:23], v["rnti"], mbps(v["dl_brate"]), mbps(v["dl_sched_brate"]),
                     num(round_half_up(v["dl_mcs"])), num(v["dl_nof_ok"]), num(v["dl_nof_nok"]), num(v["dl_nof_dtx"]),
                     num(v["dl_nof_retx"]), num(v["dl_nof_prbs"]), mbps(v["ul_sched_brate"]), num(v["ul_nof_retx"]),
                     num(v["ul_nof_prbs"]), num(round_half_up(v["cqi"])), num(v["dl_ri"]), num(v["sr_count"])))
    print_table(header, rows[:max_rows])
    if len(rows) > max_rows:
        print(f"... {len(rows) - max_rows} more, see --max-rows")


def print_grants(res: dict, kind: str, max_rows: int) -> None:
    header = ("time", *ue_metrics.ITEM_FIELDS[GRANT_KINDS[kind]])
    rows = sorted(res["kept"])
    print_table(header, [(ts[11:], *(num(v) for v in row)) for ts, *row in rows[:max_rows]])
    if len(rows) > max_rows:
        print(f"... {len(rows) - max_rows} more, see --max-rows")


def print_table(header: tuple, rows: list[tuple]) -> None:
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows)]
    for row in (header, *rows):
        print("  ".join(str(c).rjust(w) for c, w in zip(row, widths)).rstrip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--rnti", action="append", default=[])
    ap.add_argument("--ue", action="append", type=int, default=[])
    ap.add_argument("--after")
    ap.add_argument("--before")
    ap.add_argument("--period", type=float, default=1.0)
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--grants", choices=sorted(GRANT_KINDS))
    ap.add_argument("--max-rows", type=int, default=200)
    ap.add_argument("-j", type=int, default=None)
    args = ap.parse_args()

    rntis = [parse_any(r) for r in args.rnti]
    if None in rntis:
        ap.error(f"invalid RNTI in {args.rnti}, expected 4601, 0x4601 or 17921")
    if not math.isfinite(args.period) or args.period <= 0:
        ap.error("--period must be finite and positive")
    opts = {"rntis": rntis, "ues": args.ue, "after": iso_bound(args.after), "before": iso_bound(args.before),
            "period": args.period,
            "grants": args.grants}

    status = 0
    try:
        paths = traces_in(args.path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for path in paths:
        workers = args.j
        if workers is None:
            workers = min(MAX_DEFAULT_WORKERS, os.cpu_count() or 1) if path.stat().st_size >= PARALLEL_MIN_SIZE else 1
        try:
            res = read_trace(path, workers, opts)
        # FormatError, of an unsupported trace, is a ValueError too, as is a trace without its start event.
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            status = 1
            continue
        print(f"== {path}")
        if not res["nof_slots"]:
            print("no slots in the window")
            continue
        if (rntis or args.ue) and not res["agg"].ues:
            print("no grants or reports of the filtered UEs in the window")
            continue
        print_summary(res)
        if args.summary:
            continue
        print()
        if args.grants:
            print_grants(res, args.grants, args.max_rows)
        else:
            print_periods(res, args.max_rows)
    return status


if __name__ == "__main__":
    sys.exit(main())
