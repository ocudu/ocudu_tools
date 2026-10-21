#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
schedtrace_harq.py — HARQ transactions of UEs, from OCUDU scheduler event traces.

Decodes schedtrace_cell<N>.bin with the `parsers` package of this repo and groups, per HARQ process of a UE, the life
of each transport block: its new transmission and retransmissions (PDSCH or PUSCH), the PDCCHs that schedule them,
the UCI of their HARQ-ACK (PUCCH, or a PUSCH of the UE in the UCI slot, inferred) and the HARQ-ACK reports, until an
ACK or the next new transmission on the process. Prints the transactions of an allocation, or those of DL transport
blocks never ACKed. The trace has no UL CRC, so UL transactions have no outcome, and no event of HARQ processes that
time out: a DL transaction without ACK ends at the next new transmission on its process.

Usage:
  python3 schedtrace_harq.py <path> --rnti 0x4601 --harq-id 3 --slot 263.3        # transactions with a PDSCH or
                                                                                  # PUSCH at that SFN.slot
  python3 schedtrace_harq.py <path> --rnti 0x4601 --harq-id 3 --after 13:51:52.38 --dir dl
  python3 schedtrace_harq.py <path> --failed [--rnti 0x4601]

<path>: a schedtrace_cell<N>.bin, or a directory, whose schedtrace_cell*.bin are all read.

Options:
  --rnti <R>          Keep this UE, by RNTI in any form: 4601 (hex as logs print it), 0x4601, or 17921 (decimal as
                      F1AP pcaps hold it). Repeatable.
  --harq-id <H>       Keep this HARQ process.
  --dir <D>           Keep dl or ul transactions.
  --slot <S.s>        Keep transactions with a PDSCH or PUSCH at this SFN.slot, of any SFN cycle.
  --after <T>         Keep transactions with a PDSCH or PUSCH at or after T: an ISO time (2026-10-12T13:35:40 or
                      2026-10-12 13:35:40), a date, or a time of day (13:35:40.5), compared to the same precision, UTC.
  --before <T>        Keep transactions with a PDSCH or PUSCH at or before T, as --after.
  --failed            List the DL transactions without ACK, one per row, instead of their PDUs and reports.
  --max <N>           Transactions printed at most per trace (default: 5, or 200 with --failed).
"""

from __future__ import annotations

import argparse
import mmap
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))

from parsers import schedtrace  # noqa: E402
from parsers.log.search import iso_bound  # noqa: E402
from parsers.ran.rnti import normalize, parse_any  # noqa: E402
from parsers.schedtrace import grid, harq  # noqa: E402

# Kinds of the rows of the transmissions of a transaction.
TX_KINDS = (harq.PDSCH, harq.PUSCH)


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


def slot_text(slot_tx: int, spf: int) -> str:
    return "{}.{}".format(*divmod(slot_tx, spf))


def matches(tr: harq.Transaction, opts: dict, spf: int) -> bool:
    """Whether a transaction passes the filters: of its UE, process and direction, and with a transmission at the slot
    and in the time window.
    """
    if opts["rntis"] and tr.rnti not in opts["rntis"]:
        return False
    if opts["harq_id"] is not None and tr.harq_id != opts["harq_id"]:
        return False
    if opts["dir"] and tr.dir != opts["dir"]:
        return False
    if opts["failed"] and (tr.dir != "DL" or tr.acked):
        return False
    txs = [r for r in tr.rows if r.kind in TX_KINDS]
    if opts["slot"] and not any(slot_text(r.slot_tx, spf) == opts["slot"] for r in txs):
        return False
    return any(in_window(time_text(r.t), opts["after"], opts["before"]) for r in txs)


def read_trace(path: Path, opts: dict) -> tuple[list[harq.Transaction], int, int]:
    """The transactions of a trace that pass the filters, at most opts["max"], how many more pass, and the slots per
    frame of its cell.
    """
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        cell = grid.cell_of(schedtrace.start_event(mm, str(path), grid_only=True))
    spf = round(0.01 / cell.slot_duration_s)
    kept, more = [], 0
    for tr in harq.transactions(enumerate(schedtrace.read(path, grid_only=True), 1), spf):
        if not matches(tr, opts, spf):
            continue
        if len(kept) < opts["max"]:
            kept.append(tr)
        else:
            more += 1
    # Transactions end in the order of their ends, printed in the order of their starts.
    kept.sort(key=lambda tr: tr.rows[0].event)
    return kept, more, spf


def params_text(params: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items() if v is not None)


def print_transaction(tr: harq.Transaction, spf: int) -> None:
    ue = "" if tr.ue_index is None else f" ue={tr.ue_index}"
    started = "" if tr.started else " (new transmission before the trace)"
    print(f"{tr.dir} rnti={tr.rnti}{ue} harq_id={tr.harq_id} tbs={tr.tbs}: {tr.outcome()}{started}")
    rows = [(time_text(r.t)[11:], slot_text(r.slot_tx, spf), r.kind, r.tx, params_text(r.params)) for r in tr.rows]
    print_table(("time", "slot", "kind", "tx", "parameters"), rows)


def print_failed(trs: list[harq.Transaction], spf: int) -> None:
    rows = []
    for tr in trs:
        first = next(r for r in tr.rows if r.kind in TX_KINDS)
        rows.append((time_text(first.t)[11:], slot_text(first.slot_tx, spf), tr.rnti, tr.harq_id, tr.tbs, tr.nof_tx,
                     ",".join(tr.statuses) or "-", tr.outcome()))
    print_table(("time", "slot", "rnti", "harq_id", "tbs", "txs", "reports", "outcome"), rows)


def print_table(header: tuple, rows: list[tuple]) -> None:
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows)]
    for row in (header, *rows):
        # The last column, free text, is left aligned.
        cells = [str(c).rjust(w) for c, w in zip(row[:-1], widths)] + [str(row[-1])]
        print("  ".join(cells).rstrip())


def error_text(path: Path, e: Exception) -> str:
    """The message of an error of a trace, which starts with its path."""
    return str(e) if str(e).startswith(str(path)) else f"{path}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--rnti", action="append", default=[])
    ap.add_argument("--harq-id", type=int)
    ap.add_argument("--dir", choices=("dl", "ul"))
    ap.add_argument("--slot")
    ap.add_argument("--after")
    ap.add_argument("--before")
    ap.add_argument("--failed", action="store_true")
    ap.add_argument("--max", type=int)
    args = ap.parse_args()

    rntis = [parse_any(r) for r in args.rnti]
    if None in rntis:
        ap.error(f"invalid RNTI in {args.rnti}, expected 4601, 0x4601 or 17921")
    if args.slot and not re.fullmatch(r"\d+\.\d+", args.slot):
        ap.error("--slot must be SFN.slot, e.g. 263.3")
    if args.failed and args.dir == "ul":
        ap.error("--failed lists DL transactions only: the trace has no UL CRC")
    if not args.failed and not (args.rnti and args.harq_id is not None):
        ap.error("give --rnti and --harq-id, or --failed")
    opts = {"rntis": {normalize(r) for r in rntis}, "harq_id": args.harq_id,
            "dir": args.dir.upper() if args.dir else None, "slot": args.slot, "after": iso_bound(args.after),
            "before": iso_bound(args.before), "failed": args.failed,
            "max": args.max if args.max is not None else 200 if args.failed else 5}

    status = 0
    try:
        paths = traces_in(args.path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for path in paths:
        try:
            kept, more, spf = read_trace(path, opts)
        # FormatError, of an unsupported trace, is a ValueError too, as is a trace without its start event.
        except ValueError as e:
            print(f"ERROR: {error_text(path, e)}", file=sys.stderr)
            status = 1
            continue
        print(f"== {path}")
        if not kept:
            print("no transactions match")
            continue
        if args.failed:
            print_failed(kept, spf)
        else:
            for i, tr in enumerate(kept):
                if i:
                    print()
                print_transaction(tr, spf)
        if more:
            print(f"... {more} more, see --max")
    return status


if __name__ == "__main__":
    sys.exit(main())
