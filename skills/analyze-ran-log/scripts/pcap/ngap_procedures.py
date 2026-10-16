#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Per-UE chronological NGAP procedure sequence.

Usage:
    ngap_procedures.py <ngap.pcap>
    ngap_procedures.py <ngap.pcap> --ue 42
    ngap_procedures.py <ngap.pcap> --failures-only
    ngap_procedures.py <ngap.pcap> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import utils
from parsers.pcap import ngap
from parsers.ran.procedures import proc_name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", help="path to ngap.pcap")
    ap.add_argument("--ue", help="filter to one RAN-UE-NGAP-ID")
    ap.add_argument("--failures-only", action="store_true",
                    help="only show unsuccessful outcomes; successful ones may also carry a cause")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    pcap = Path(args.pcap)
    if not pcap.is_file():
        print(f"error: not a file: {pcap}", file=sys.stderr)
        return 1
    try:
        by_ue = ngap.procedures(utils.TSHARK, pcap, ue=args.ue, failures_only=args.failures_only)
    except utils.TsharkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(by_ue, indent=2))
        return 0

    if not by_ue:
        print("(no matching NGAP rows)")
        return 0
    for ue, events in by_ue.items():
        print(f"== RAN-UE-NGAP-ID={ue}  ({len(events)} events)")
        for e in events:
            marker = "!" if e["failure"] else " "
            cause = f"  cause={e['cause']}" if e["cause"] else ""
            print(
                f"  {marker} frame={e['frame']:>5} {e['iso']}  "
                f"{proc_name('ngap', e['procedureCode']):<32}"
                f"  amfUeId={e['amfUeId'] or '-'}{cause}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
