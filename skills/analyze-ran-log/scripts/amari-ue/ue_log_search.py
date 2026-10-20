#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
ue_log_search.py - Search Amarisoft UE log with filtering.

Handles multi-line log blocks (RRC/NAS PDU bodies) correctly —
a filter that matches anywhere in a block (header + body) will
return the entire block.

Usage:
  python3 ue_log_search.py <path> [options]

<path>: a UE log file or any directory (resolved the same way as ue_log_summary.py)

Options:
  --layer <LAYER>       Filter by log layer: NAS, RRC, PHY, MAC, RLC, PDCP, PROD, TRX
  --ue <ue_id>          Filter by UE ID (4-char hex, e.g. 0001, 000a, 0080)
  --cell <cell_id>      Filter by cell index (2-char decimal, e.g. 00, 01)
  --after <ts>          Only include lines at or after HH:MM:SS.mmm
  --before <ts>         Only include lines at or before HH:MM:SS.mmm
                        (a log crossing midnight: each time is its first
                        occurrence at or after the first log line)
  --pattern <regex>     Additional regex, case-insensitive (searched across entire block)
  --count               Print match count only (no block content)
  --max-lines <N>       Max output lines before truncation (default: 200)

Examples:
  # All NAS state transitions
  python3 ue_log_search.py ue.log --layer NAS --pattern "New state"

  # RRC messages for UE 0001 after a specific time
  python3 ue_log_search.py ue.log --layer RRC --ue 0001 --after 12:24:34.000

  # Count PRACH attempts
  python3 ue_log_search.py ue.log --layer PHY --pattern "PRACH:" --count

  # Handovers (match the ASN.1 body structure, not the bare word, which also
  # appears in diagnostic log lines)
  python3 ue_log_search.py ue.log --pattern "reconfigurationWithSync {" --count

  # PHY CRC failures in a time window
  python3 ue_log_search.py ue.log --layer PHY --pattern "crc=FAIL" \\
      --after 12:24:34.000 --before 12:24:35.000
"""

import argparse
import re
import sys

import ue_log_summary  # reuse resolve_ue_log()


LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d{3}) \[(\w+)\]")
HMS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)$")
DAY = 86400.0
HALF_DAY = DAY / 2


def hms_seconds(s: str) -> float:
    m = HMS_RE.match(s)
    if not m:
        raise ValueError(s)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _hms_arg(s: str) -> float:
    try:
        return hms_seconds(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not HH:MM:SS[.mmm]: {s!r}")


def _regex_arg(s: str) -> re.Pattern:
    try:
        return re.compile(s, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"invalid regex {s!r}: {e}")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Search Amarisoft UE log with filtering",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", help="ue.log file or containing directory")
    ap.add_argument("--layer", help="Layer filter (NAS, RRC, PHY, MAC, RLC, PDCP, PROD, TRX)")
    ap.add_argument("--ue", help="UE ID filter (4-char hex, e.g. 0001, 000a)")
    ap.add_argument("--cell", help="Cell ID filter (e.g. 00, 01)")
    ap.add_argument("--after", type=_hms_arg,
                    help="Start timestamp HH:MM:SS.mmm (inclusive)")
    ap.add_argument("--before", type=_hms_arg,
                    help="End timestamp HH:MM:SS.mmm (inclusive)")
    ap.add_argument("--pattern", type=_regex_arg,
                    help="Regex pattern, case-insensitive (searched across full block)")
    ap.add_argument("--count", action="store_true", help="Print count only")
    ap.add_argument("--max-lines", type=int, default=200,
                    help="Max output lines before truncation (default: 200)")
    return ap.parse_args(argv)


class LogClock:
    """Seconds since the first log day of the HH:MM:SS.mmm line times, read in file order."""

    def __init__(self):
        self.first: float | None = None
        self._day = 0.0
        self._last: float | None = None

    def elapsed(self, ts: str) -> float:
        sec = hms_seconds(ts)
        if self.first is None:
            self.first = sec
        # The line clock has no date, so a time far behind the previous one crossed midnight.
        elif self._last - sec > HALF_DAY:
            self._day += DAY
        self._last = sec
        return self._day + sec

    def bound(self, sec: float) -> float:
        """A --after/--before time placed at its first occurrence at or after the first line."""
        return sec if sec >= self.first else sec + DAY


def block_matches(header_line: str, body_lines: list[str], args, clock: LogClock) -> bool:
    ts_m = LINE_RE.match(header_line)
    if not ts_m:
        return False
    ts = ts_m.group(1)
    layer = ts_m.group(2)

    # Time range
    t = clock.elapsed(ts)
    if args.after is not None and t < clock.bound(args.after):
        return False
    if args.before is not None and t > clock.bound(args.before):
        return False

    # Layer filter
    if args.layer and layer.upper() != args.layer.upper():
        return False

    # Assemble full text (header + body) for content searches
    full_text = header_line
    if body_lines:
        full_text += "\n" + "\n".join(body_lines)

    # Positional field match. Fields after the "[LAYER]" tag are:
    #   <dir> <ue_id> <cell_id> ...   (ue_id is the pseudo-id `f000` on broadcast
    #   RRC lines, a 4-char hex UE id otherwise).
    # Matching by position avoids false hits (e.g. substring "00" inside an RNTI).
    after = header_line.split("]", 1)[1].split() if "]" in header_line else []
    ue_field = after[1] if len(after) > 1 else None
    cell_field = after[2] if len(after) > 2 else None

    if args.ue and ue_field != args.ue:
        return False
    if args.cell and cell_field != args.cell:
        return False

    # Pattern filter (across entire block, multiline so ^ and $ work per line)
    if args.pattern and not args.pattern.search(full_text):
        return False

    return True


def search(args):
    log_path = ue_log_summary.resolve_ue_log(args.path)
    clock = LogClock()

    output_blocks = []

    current_header = None
    current_body = []

    def process_block():
        nonlocal current_header, current_body
        if current_header is None:
            return
        if block_matches(current_header, current_body, args, clock):
            block_text = current_header
            if current_body:
                block_text += "\n" + "\n".join(current_body)
            output_blocks.append(block_text)
        current_header = None
        current_body = []

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            if line.startswith("#"):
                continue

            if LINE_RE.match(line):
                process_block()
                current_header = line
                current_body = []
            else:
                if current_header is not None:
                    current_body.append(line)

        process_block()  # flush last block

    if args.count:
        print(len(output_blocks))
        return

    # Print whole blocks only; stop before exceeding the line cap (always show ≥1).
    output_lines = 0
    shown_blocks = 0
    for block in output_blocks:
        n_lines = block.count("\n") + 2  # block lines + blank separator
        if shown_blocks > 0 and output_lines + n_lines > args.max_lines:
            break
        print(block)
        print()
        output_lines += n_lines
        shown_blocks += 1

    if shown_blocks < len(output_blocks):
        remaining = len(output_blocks) - shown_blocks
        print(f"... {remaining} more matching block(s) not shown (stopped near the "
              f"{args.max_lines}-line cap). Narrow with --after/--before/--ue/--cell.")


if __name__ == "__main__":
    args = parse_args()
    try:
        search(args)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
