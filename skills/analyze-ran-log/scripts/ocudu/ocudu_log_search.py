#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
ocudu_log_search.py — Search an OCUDU gNB log with filtering.

Handles multi-line log blocks (RRC ASN.1 bodies, SIB1 dumps) correctly —
a filter that matches anywhere in a block (header + body) will return the
entire block.

Skips the CONFIG echo at the top of the file by default (everything before
the first timestamped line).

Usage:
  python3 ocudu_log_search.py <path> [options]

<path>: an OCUDU app log (gnb.log, du.log, cu*.log) or any directory (resolved the
same way as ocudu_log_summary.py). Filtering is done by parsers.log.search of this
repo.

Options:
  --layer <LAYER>       Filter by log layer (e.g. RRC, NGAP, CU-CP, CU-CP-F1,
                        CU-CP-E1, CU-UP-E1, DU-F1, MAC, SCHED, PHY, PDCP, GTPU,
                        DU-MNG, METRICS, GNB, CONFIG, ALL). Case-insensitive,
                        matches the bracketed layer tag exactly (stripped of
                        padding spaces).
  --level <REGEX>       Filter by level (D, I, W, E, C). Multiple via regex,
                        e.g. "E|C|W".
  --ue <N>              Filter by `ue=N` token in the message.
  --rnti <RNTI>         Filter by `rnti=0x..` / `c-rnti=0x..` tokens. Any form:
                        4601 (hex), 0x4601, or 17921 (decimal, as pcaps hold it).
  --pci <N>             Filter by `pci=N` token.
  --after <ts>          Only include lines at or after the time: an ISO
                        timestamp, date and time joined by T or a space
                        (2026-10-12T13:35:40 or "2026-10-12 13:35:40"), a date
                        (2026-10-12), or a time of day (13:35:40.5), compared
                        to the same precision, the time of day to the time
                        portion only.
  --before <ts>         Symmetric to --after.
  --pattern <regex>     Additional regex pattern (searched across the entire
                        block, case-insensitive, multiline).
  --count               Print match count only (no block content).
  --max-lines <N>       Max output lines before truncation (default: 200).
  --include-config-echo Include the CONFIG echo at the top of the file (off by
                        default — it's the user's `ocudu_gnb.yml` plus
                        defaults, and adds no signal beyond the YAML).

Examples:
  # All RRC messages
  python3 ocudu_log_search.py gnb.log --layer RRC

  # NGAP procedures for ue=0
  python3 ocudu_log_search.py gnb.log --layer NGAP --ue 0

  # Count handovers
  python3 ocudu_log_search.py gnb.log --pattern "reconfigurationWithSync \\{" --count

  # PHY CRC failures in a window (partial timestamp form OK)
  python3 ocudu_log_search.py gnb.log --layer PHY --pattern "crc=KO" \\
      --after 18:18:30.000 --before 18:18:31.000

  # Errors and warnings across the run
  python3 ocudu_log_search.py gnb.log --level "E|C|W"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))

from parsers.log import run, search as log_search  # noqa: E402


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Search OCUDU gNB log with filtering",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", help="gnb.log file or containing directory")
    ap.add_argument("--layer", help="Layer filter (e.g. RRC, NGAP, CU-CP)")
    ap.add_argument("--level", help="Log-level regex (D|I|W|E)")
    ap.add_argument("--ue", help="Match `ue=N` in the message")
    ap.add_argument("--rnti", help="Match `rnti=0x..` tokens; 4601, 0x4601 or 17921")
    ap.add_argument("--pci", help="Match `pci=N` in the message")
    ap.add_argument("--after", help="Start time: ISO timestamp (T or space), date, or HH:MM:SS[.ffffff]")
    ap.add_argument("--before", help="End time: ISO timestamp (T or space), date, or HH:MM:SS[.ffffff]")
    ap.add_argument("--pattern", help="Regex pattern (full block, multiline, IGNORECASE)")
    ap.add_argument("--count", action="store_true", help="Print count only")
    ap.add_argument("--max-lines", type=int, default=200,
                    help="Max output lines before truncation (default: 200)")
    ap.add_argument("--include-config-echo", action="store_true",
                    help="Include the CONFIG echo at the top of the file")
    return ap.parse_args(argv)


def search(args):
    log_path = run.find_log(args.path)
    flt = log_search.Filter(layer=args.layer, level=args.level, ue=args.ue, rnti=args.rnti, pci=args.pci,
                            after=args.after, before=args.before, pattern=args.pattern)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        matches = list(log_search.search(f, flt, args.include_config_echo))

    if args.count:
        print(len(matches))
        return
    # On stderr, so that the output of no match stays empty.
    if not matches:
        print("no entries match the filters", file=sys.stderr)
        return

    output_lines = 0
    shown = 0
    for block in matches:
        n_lines = block.count("\n") + 2
        if shown > 0 and output_lines + n_lines > args.max_lines:
            break
        print(block)
        print()
        output_lines += n_lines
        shown += 1

    if shown < len(matches):
        remaining = len(matches) - shown
        print(f"... {remaining} more matching block(s) not shown (stopped near the "
              f"{args.max_lines}-line cap). Narrow with --after/--before/--ue/--rnti/--pci.")


if __name__ == "__main__":
    args = parse_args()
    try:
        search(args)
    # A missing log, an invalid --rnti, or an invalid --pattern or --level regex.
    except (FileNotFoundError, ValueError, re.error) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
