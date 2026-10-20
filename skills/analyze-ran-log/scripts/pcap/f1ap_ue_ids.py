#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Per-UE F1AP identity table.

Clusters F1AP rows by the CU-assigned `gNB-CU-UE-F1AP-ID` when available,
falling back to the DU-assigned `gNB-DU-UE-F1AP-ID` for rows from
InitialULRRCMessageTransfer (where the CU ID is not yet set). Each output row
is one UE seen in F1AP, with the frame and timestamp of the first sighting.

Usage:
    f1ap_ue_ids.py <f1ap.pcap>
    f1ap_ue_ids.py <f1ap.pcap> --ue 5          # UEs of this CU or DU F1AP id
    f1ap_ue_ids.py <f1ap.pcap> --rnti 4601     # UEs of this C-RNTI, in any form: 4601, 0x4601, 17921
    f1ap_ue_ids.py <f1ap.pcap> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import utils
from parsers.pcap import f1ap
from parsers.ran.rnti import normalize, parse_any


def main(argv: list[str] | None = None, limit_flag: str = "--limit") -> int:
    """Runs the script with argv; limit_flag is the flag of the row cap that the truncation hint names."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", help="path to f1ap.pcap")
    ap.add_argument("--ue", help="keep the UEs of this gNB-CU or gNB-DU UE F1AP id")
    ap.add_argument("--rnti", help="keep the UEs of this C-RNTI, in any form: 4601 (hex as logs print it), 0x4601, "
                                   "or 17921 (decimal as the pcap holds it)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap text output rows (default 0 = no cap; set a positive N to cap)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass /tmp tshark-extraction cache and re-run tshark")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rnti = parse_any(args.rnti) if args.rnti else None
    if args.rnti and rnti is None:
        ap.error(f"not an RNTI: {args.rnti!r}")
    pcap = Path(args.pcap)
    if not pcap.is_file():
        print(f"error: not a file: {pcap}", file=sys.stderr)
        return 1
    try:
        out_list = f1ap.ue_ids(utils.TSHARK, pcap, force=args.no_cache)
    except utils.TsharkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.ue:
        out_list = [r for r in out_list if args.ue == r["cu_ue_f1ap_id"] or args.ue in r["du_ue_f1ap_ids"]]
    if rnti:
        out_list = [r for r in out_list if any(normalize(c) == rnti for c in r["crntis"])]

    if args.json:
        print(json.dumps(out_list, indent=2))
        return 0

    if not out_list:
        print("(no F1AP UEs matched)")
        return 0
    header = ("frame", "first_iso", "message", "cu_ue_f1ap_id", "du_ue_f1ap_ids", "crntis")
    print(", ".join(header))
    rendered = out_list if args.limit <= 0 else out_list[: args.limit]
    for r in rendered:
        print(", ".join([
            str(r["frame"]) if r["frame"] is not None else "-",
            r["first_iso"],
            r["message"],
            r["cu_ue_f1ap_id"] or "-",
            ";".join(r["du_ue_f1ap_ids"]) or "-",
            ";".join(r["crntis"]) or "-",
        ]))
    if args.limit > 0 and len(out_list) > args.limit:
        print(f"... {len(out_list) - args.limit} more rows truncated (use {limit_flag} 0 to show all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
