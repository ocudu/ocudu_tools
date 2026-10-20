#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Per-UE E1AP identity table.

Clusters E1AP rows by `gNB-CU-CP-UE-E1AP-ID`, attaching `gNB-CU-UP-UE-E1AP-ID`
once the CU-UP responds. Infrastructure messages (E1Setup, status
indications, ...) that carry no per-UE IDs are skipped.

Usage:
    e1ap_ue_ids.py <e1ap.pcap>
    e1ap_ue_ids.py <e1ap.pcap> --ue 7   # filter by any identifier value
    e1ap_ue_ids.py <e1ap.pcap> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import utils
from parsers.pcap import e1ap


def main(argv: list[str] | None = None, limit_flag: str = "--limit") -> int:
    """Runs the script with argv; limit_flag is the flag of the row cap that the truncation hint names."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", help="path to e1ap.pcap")
    ap.add_argument("--ue", help="filter rows mentioning this identifier value")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap text output rows (default 0 = no cap; set a positive N to cap)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass /tmp tshark-extraction cache and re-run tshark")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    pcap = Path(args.pcap)
    if not pcap.is_file():
        print(f"error: not a file: {pcap}", file=sys.stderr)
        return 1
    try:
        out_list = e1ap.ue_ids(utils.TSHARK, pcap, force=args.no_cache)
    except utils.TsharkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.ue:
        out_list = [r for r in out_list if args.ue in (r["e1_cp_ue_id"], r["e1_up_ue_id"])]

    if args.json:
        print(json.dumps(out_list, indent=2))
        return 0

    if not out_list:
        print("(no E1AP UEs matched)")
        return 0
    header = ("frame", "first_iso", "message", "e1_cp_ue_id", "e1_up_ue_id")
    print(", ".join(header))
    rendered = out_list if args.limit <= 0 else out_list[: args.limit]
    for r in rendered:
        print(", ".join([
            str(r["frame"]) if r["frame"] is not None else "-",
            r["first_iso"],
            r["message"],
            r["e1_cp_ue_id"] or "-",
            r["e1_up_ue_id"] or "-",
        ]))
    if args.limit > 0 and len(out_list) > args.limit:
        print(f"... {len(out_list) - args.limit} more rows truncated (use {limit_flag} 0 to show all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
