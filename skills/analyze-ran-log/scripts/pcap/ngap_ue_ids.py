#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Per-UE NGAP identity table.

Clusters NGAP rows by `RAN-UE-NGAP-ID`, attaching `AMF-UE-NGAP-ID` once the
AMF assigns it (from InitialContextSetupRequest onwards). Infrastructure
messages (NGSetup, AMFConfigurationUpdate, ...) that don't reference a UE
are skipped.

Usage:
    ngap_ue_ids.py <ngap.pcap>
    ngap_ue_ids.py <ngap.pcap> --ue 42  # filter by any identifier value
    ngap_ue_ids.py <ngap.pcap> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import utils
from parsers.pcap import ngap


def main(argv: list[str] | None = None, limit_flag: str = "--limit") -> int:
    """Runs the script with argv; limit_flag is the flag of the row cap that the truncation hint names."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", help="path to ngap.pcap")
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
        out_list = ngap.ue_ids(utils.TSHARK, pcap, force=args.no_cache)
    except utils.TsharkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.ue:
        out_list = [r for r in out_list if args.ue in (r["ran_ue_ngap_id"], r["amf_ue_ngap_id"])]

    if args.json:
        print(json.dumps(out_list, indent=2))
        return 0

    if not out_list:
        print("(no NGAP UEs matched)")
        return 0
    header = ("frame", "first_iso", "message", "ran_ue_ngap_id", "amf_ue_ngap_id")
    print(", ".join(header))
    rendered = out_list if args.limit <= 0 else out_list[: args.limit]
    for r in rendered:
        print(", ".join([
            str(r["frame"]) if r["frame"] is not None else "-",
            r["first_iso"],
            r["message"],
            r["ran_ue_ngap_id"] or "-",
            r["amf_ue_ngap_id"] or "-",
        ]))
    if args.limit > 0 and len(out_list) > args.limit:
        print(f"... {len(out_list) - args.limit} more rows truncated (use {limit_flag} 0 to show all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
