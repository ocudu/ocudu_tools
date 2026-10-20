#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""One-shot summary for an OCUDU run directory.

Calls pcap_overview.py, then the three per-protocol UE-ID scripts for whichever
sibling pcaps are present, and prints a unified summary block. Equivalent to
running pcap_overview + f1ap_ue_ids + ngap_ue_ids + e1ap_ue_ids by hand.

Usage:
    summary.py <run-dir>
    summary.py <run-dir> --top 5
    summary.py <run-dir> --ue-limit 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Puts the parsers package on the import path.
import utils  # noqa: F401

import e1ap_ue_ids
import f1ap_ue_ids
import ngap_ue_ids
import pcap_overview
from parsers.pcap.run import PROTOCOLS, find_pcaps, is_run_dir


def _section(title: str) -> None:
    print(f"\n## {title}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="OCUDU run directory containing sibling pcaps")
    ap.add_argument("--top", type=int, default=5,
                    help="top N procedure codes per pcap (pcap_overview)")
    ap.add_argument("--ue-limit", type=int, default=20,
                    help="cap UE-ID rows per protocol (default 20; use 0 for no cap)")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not is_run_dir(run_dir):
        print(f"error: not a run directory (need pcaps of ≥2 of {PROTOCOLS}): {run_dir}", file=sys.stderr)
        return 1

    pcaps = find_pcaps(run_dir)

    _section("pcap overview")
    rc = pcap_overview.main([str(run_dir), "--top", str(args.top)])
    if rc != 0:
        return rc

    for proto, mod, pcap in (
        ("F1AP", f1ap_ue_ids, pcaps["f1ap"]),
        ("NGAP", ngap_ue_ids, pcaps["ngap"]),
        ("E1AP", e1ap_ue_ids, pcaps["e1ap"]),
    ):
        if pcap is None:
            continue
        _section(f"{proto} UEs")
        mod.main([str(pcap), "--limit", str(args.ue_limit)], limit_flag="--ue-limit")

    return 0


if __name__ == "__main__":
    sys.exit(main())
