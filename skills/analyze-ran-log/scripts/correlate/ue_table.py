#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
ue_table.py — The UEs of an OCUDU run, with all their identifiers: one row per UE context, or per UE trace.

Combines the UE contexts of the F1AP pcap and of the OCUDU log of a run, and follows each UE through its contexts and
its NGAP and E1AP contexts, with the `parsers` package of this repo (parsers.correlate):
  - an F1AP UE context (du_f1ap, cu_f1ap, C-RNTI) joins the log UE context (DU UE index `du_ue`, CU-CP UE index
    `cu_ue`, RNTI) of the same RNTI with overlapping lifetimes;
  - a UE trace (`ue_trace`) chains the contexts of a UE through handovers (target C-RNTI) and reestablishments (old
    C-RNTI), and joins its NGAP context (ran_ngap, amf_ngap) by NAS PDU or handover C-RNTI and its E1AP context
    (cu_cp_e1ap, cu_up_e1ap) by UPF TEID.
Each context row is one cell of a UE: a handover or reestablishment gives a new row of the same trace.

Usage:
  python3 ue_table.py <path>                          # all UE contexts of the run
  python3 ue_table.py <path> --traces                 # one row per UE trace
  python3 ue_table.py <path> --where rnti=4602        # the context of an RNTI, in any form: 4602, 0x4602, 17922
  python3 ue_table.py <path> --where ue_trace=3       # the contexts of a UE
  python3 ue_table.py <path> --where ran_ngap=7 --traces
  python3 ue_table.py <path> --after 12:24:34 --before 12:24:40 --csv

<path>: a run dir, component dir or test dir, resolved like ocudu_log_summary.py, or the log itself. A pcap or
config file of a run stands for its run dir.

Options:
  --where <k=v>       Keep rows with identifier k equal to v: rnti, du_ue, cu_ue, ue_trace, du_f1ap, cu_f1ap, ran_ngap,
                      amf_ngap, cu_cp_e1ap, cu_up_e1ap. rnti in any form: 4601 (hex as logs print it), 0x4601, or
                      17921 (decimal as F1AP pcaps hold it). Repeatable.
  --after/--before    Keep rows alive in a window, as an ISO timestamp (date and time joined by T or a space), a date,
                      or a HH:MM:SS[.ffffff] time of day, compared to the same precision, UTC.
  --traces            One row per UE trace rather than per UE context.
  --with-ra           Also list the random accesses of no UE (a PRACH in the log only, e.g. a failed RA).
  --log <file>        Log to use instead of the one of the run, e.g. of no standard name. Its directory is the run
                      dir when <path> does not exist.
  --f1ap <file>       F1AP pcap to use instead of the one of the run. Without one, the UEs are the log ones only.
  --csv               Print CSV, with all the columns. The text table leaves out the columns no row has.

The NGAP and E1AP pcaps of the run, <proto>.pcap or with a prefix, e.g. gnb_ngap.pcap, are used when present. The DU
and CU-CP UE indexes are different ids, both reused soon after a UE is released; RNTIs and protocol UE ids are not, so
prefer them as keys.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))

from parsers.correlate import run  # noqa: E402
from parsers.correlate.ues import Ue, UeTrace  # noqa: E402
from parsers.log.run import LOG_NAMES, primary_log_in, resolve_run_dir  # noqa: E402
from parsers.log.search import iso_bound  # noqa: E402
from parsers.pcap.run import find_pcaps, pcap_proto  # noqa: E402
from parsers.pcap.tshark import Tshark  # noqa: E402
from parsers.ran.rnti import parse_any as parse_rnti  # noqa: E402

ID_COLUMNS = ("ue_trace", "rnti", "du_ue", "cu_ue", "du_f1ap", "cu_f1ap", "ran_ngap", "amf_ngap", "cu_cp_e1ap", "cu_up_e1ap")
COLUMNS = ("start", "end", "duration_s", *ID_COLUMNS, "sources")
TRACE_COLUMNS = ("start", "end", "duration_s", "contexts", *ID_COLUMNS)
# Suffixes of the files of a run that are not logs.
OTHER_SUFFIXES = (".pcap", ".pcapng", ".yml", ".yaml", ".json", ".bin", ".cfg", ".conf", ".gz", ".bz2", ".xz", ".tar")


def utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def time_bound(ref: str, epoch: float) -> str:
    """The form of a timestamp that compares with a --after/--before value, normalized by iso_bound()."""
    t = utc(epoch)
    # Cut to the precision of the bound, so that both bound forms are inclusive alike.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref[:10]):
        return t.replace(tzinfo=None).isoformat(timespec="microseconds")[: len(ref)]
    return t.strftime("%H:%M:%S.%f")[: len(ref)]


def resolve_inputs(path_str: str, log_str: str | None) -> tuple[Path, Path]:
    """The run dir and the log of a <path> and --log. Raises FileNotFoundError without them."""
    path = Path(path_str)
    log = Path(log_str) if log_str else None
    if log is not None and not log.is_file():
        raise FileNotFoundError(f"no log file {log}")
    if path.is_file():
        run_dir = path.parent
        if log is None and not is_other_artifact(path):
            log = path
    elif path.is_dir():
        try:
            run_dir = resolve_run_dir(path)
        except FileNotFoundError:
            # A run whose log has no standard name, given with --log.
            if log is None:
                raise
            run_dir = path
    elif log is not None:
        run_dir = log.parent
    else:
        raise FileNotFoundError(f"not found: {path}")
    log = log or primary_log_in(run_dir)
    if log is None:
        raise FileNotFoundError(f"no OCUDU log ({', '.join(LOG_NAMES)}) in {run_dir}, see --log")
    return run_dir, log


def is_other_artifact(path: Path) -> bool:
    """Whether a file of a run is not a log, e.g. a pcap or a config."""
    return pcap_proto(path) is not None or path.suffix in OTHER_SUFFIXES


def where_item(item: str) -> tuple[str, str]:
    """The identifier and value of a --where k=v, an RNTI as logs print it. Raises ValueError if invalid."""
    name, sep, value = item.partition("=")
    name, value = name.strip(), value.strip()
    if not sep:
        raise ValueError(f"--where takes k=v, not {item!r}")
    if name not in ID_COLUMNS:
        raise ValueError(f"--where key {name!r} is not one of {', '.join(ID_COLUMNS)}")
    if name == "rnti":
        rnti = parse_rnti(value)
        if rnti is None:
            raise ValueError(f"invalid RNTI {value!r} in --where, expected 4601, 0x4601 or 17921")
        value = rnti
    return name, value


def matches(ids: dict[str, list[str]], where: list[tuple[str, str]]) -> bool:
    return all(value in ids.get(name, ()) for name, value in where)


def times(t_start: float, t_end: float, is_open: bool) -> dict[str, str]:
    return {
        "start": utc(t_start).strftime("%H:%M:%S.%f")[:-3],
        "end": "open" if is_open else utc(t_end).strftime("%H:%M:%S.%f")[:-3],
        "duration_s": f"{t_end - t_start:.3f}",
    }


def row(ue: Ue, names: dict[str, str]) -> dict[str, str]:
    """A table row of a UE context, with the kinds of its sources, given the kind of each source by name."""
    sources = {source for source, _ in ue.parts}
    kinds = [kind for name, kind in names.items() if name in sources]
    return {
        **times(ue.t_start, ue.t_end, ue.open),
        **{name: ",".join(ue.ids.get(name, [])) or "-" for name in ID_COLUMNS},
        "sources": "+".join(kinds),
    }


def nof_in_both(ues: list[Ue], f1ap: Path | None, log: Path) -> int:
    """The UE contexts with parts of both the F1AP pcap and the log, as the sources column shows them."""
    return sum(1 for ue in ues if f1ap and {f1ap.name, log.name} <= {source for source, _ in ue.parts})


def short(values: list[str]) -> str:
    """Values of an identifier of a trace, the first and last of more than three, e.g. "0x4601,…,0x4618 (24)"."""
    if not values:
        return "-"
    return ",".join(values) if len(values) <= 3 else f"{values[0]},…,{values[-1]} ({len(values)})"


def trace_row(trace: UeTrace, is_open: bool) -> dict[str, str]:
    return {
        **times(trace.t_start, trace.t_end, is_open),
        "contexts": str(len(trace.ues)),
        **{name: short(trace.ids.get(name, [])) for name in ID_COLUMNS if name != "ue_trace"},
        "ue_trace": str(trace.id),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--where", action="append", default=[], metavar="K=V")
    ap.add_argument("--after")
    ap.add_argument("--before")
    ap.add_argument("--traces", action="store_true")
    ap.add_argument("--with-ra", action="store_true")
    ap.add_argument("--log")
    ap.add_argument("--f1ap")
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args(argv)

    try:
        where = [where_item(item) for item in args.where]
    except ValueError as e:
        ap.error(str(e))

    after, before = iso_bound(args.after), iso_bound(args.before)
    try:
        run_dir, log = resolve_inputs(args.path, args.log)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.f1ap and not Path(args.f1ap).is_file():
        print(f"error: no F1AP pcap {args.f1ap}", file=sys.stderr)
        return 1
    # The pcaps of the run, <proto>.pcap or with a prefix, e.g. gnb_f1ap.pcap.
    pcaps = find_pcaps(run_dir)
    f1ap = Path(args.f1ap) if args.f1ap else pcaps.get("f1ap")
    if f1ap is None:
        print(f"note: no F1AP pcap in {run_dir}; the UEs are the ones of the log only.", file=sys.stderr)

    cores = [p for p in (pcaps.get("ngap"), pcaps.get("e1ap")) if p is not None]
    ues, traces = run.run_traces(Tshark(), f1ap, [log], cores)
    # A log UE without DU UE index and F1AP context is a random access of no UE, e.g. a failed RA.
    ra_only = {ue.key for ue in ues if "du_ue" not in ue.ids and "du_f1ap" not in ue.ids}

    def alive(t_start: float, t_end: float, is_open: bool) -> bool:
        return (not after or is_open or time_bound(after, t_end) >= after) and (
            not before or time_bound(before, t_start) <= before
        )

    if args.traces:
        open_keys = {ue.key for ue in ues if ue.open}
        columns = TRACE_COLUMNS
        rows = [
            trace_row(tr, any(k in open_keys for k in tr.ues))
            for tr in traces
            if (args.with_ra or not tr.ues or any(k not in ra_only for k in tr.ues))
            and matches({**tr.ids, "ue_trace": [str(tr.id)]}, where)
            and alive(tr.t_start, tr.t_end, any(k in open_keys for k in tr.ues))
        ]
    else:
        columns = COLUMNS
        names = {**({f1ap.name: "f1ap"} if f1ap else {}), log.name: "log"}
        rows = [
            row(ue, names) for ue in ues
            if (args.with_ra or ue.key not in ra_only) and matches(ue.ids, where) and alive(ue.t_start, ue.t_end, ue.open)
        ]

    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        return 0

    print(f"run: {run_dir}")
    print(f"sources: {', '.join([f1ap.name] if f1ap else ['(no f1ap.pcap)']) }, {log.name}" + "".join(f", {c.name}" for c in cores))
    joined = nof_in_both(ues, f1ap, log)
    nof_traces = sum(1 for tr in traces if any(k not in ra_only for k in tr.ues))
    print(f"UE contexts: {len(ues) - len(ra_only)} ({joined} in both the pcap and the log), UE traces: {nof_traces}, "
          f"random accesses of no UE: {len(ra_only)}" + ("" if args.with_ra else " (hidden, see --with-ra)"))
    if not rows:
        print("no UE matches.")
        return 0
    # Columns that no row has, e.g. cu_ue when the CU logs below info, are left out.
    columns = [c for c in columns if any(r[c] != "-" for r in rows)]
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in columns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
