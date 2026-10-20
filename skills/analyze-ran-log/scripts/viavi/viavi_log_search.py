#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
viavi_log_search.py - Search a VIAVI command log with filtering.

Block-aware: a block is one timestamped line plus the untimestamped continuation
lines that follow it (e.g. an RRC event's "Cell Info:" body). A GETSTATS record
("UE ID: <id>   (Radio Context ..." or a COMBINED "UE ID: 0-32 ...") also takes
the following stat rows that carry its timestamp. A filter that matches anywhere
in a block returns the whole block.

Usage:
  python3 viavi_log_search.py <path> [options]

<path>: a *_Command_Log*.txt/.zip file or a directory (resolved like
viavi_log_summary.py — newest log, .txt preferred, .zip read in place).

Options:
  --ue <id>           Filter to blocks mentioning "UE Id:<id>" / "UE ID: <id>"
                      (not a COMBINED "UE ID: <id>-<id>" range)
  --event <substr>    Filter to "I: CMPI" event lines containing <substr>
                      (e.g. "Random Access Error", "NR REGISTRATION IND")
  --after <ts>        Only blocks at or after HH:MM:SS:mmm
  --before <ts>       Only blocks at or before HH:MM:SS:mmm
                      (each time is its first occurrence at or after the
                      first log line, so a log crossing midnight works)
  --pattern <regex>   Additional regex, case-insensitive, searched across the whole block
  --count             Print the match count only
  --max-lines <N>     Max output lines before truncation (default: 200)

Examples:
  # Every random-access error
  python3 viavi_log_search.py <log> --event "Random Access Error"

  # Everything about UE 113 in a time window
  python3 viavi_log_search.py <log> --ue 113 --after 11:19:00:000 --before 11:21:00:000

  # Count NR registration indications ("REGISTRATION IND" also counts DEREGISTRATION)
  python3 viavi_log_search.py <log> --event "NR REGISTRATION IND" --count

  # GETSTATS record of UE 0, with its stat rows
  python3 viavi_log_search.py <log> --pattern "UE ID: 0 "
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys

import viavi_log_summary  # reuse resolve_log() / open_log() / TS_RE

TS_RE = viavi_log_summary.TS_RE
HMS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(?:[:.](\d{1,3}))?$")


def _hms_arg(s: str) -> dt.time:
    m = HMS_RE.match(s)
    try:
        if not m:
            raise ValueError
        ms = int((m.group(4) or "0").ljust(3, "0"))
        return dt.time(int(m.group(1)), int(m.group(2)), int(m.group(3)), ms * 1000)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not HH:MM:SS[:mmm]: {s!r}")


def _regex_arg(s: str) -> re.Pattern:
    try:
        return re.compile(s, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"invalid regex {s!r}: {e}")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Search a VIAVI command log with filtering",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", help="command log (.txt/.zip) or containing directory")
    ap.add_argument("--ue", help="UE id filter (e.g. 113)")
    ap.add_argument("--event", help='"I: CMPI" event substring filter')
    ap.add_argument("--after", type=_hms_arg, help="start timestamp HH:MM:SS:mmm (inclusive)")
    ap.add_argument("--before", type=_hms_arg, help="end timestamp HH:MM:SS:mmm (inclusive)")
    ap.add_argument("--pattern", type=_regex_arg,
                    help="regex, case-insensitive, searched across the full block")
    ap.add_argument("--count", action="store_true", help="print match count only")
    ap.add_argument("--max-lines", type=int, default=200,
                    help="max output lines before truncation (default: 200)")
    return ap.parse_args(argv)


class TimeWindow:
    """--after/--before bounds, each placed at its first occurrence at or after the first line."""

    def __init__(self, after: dt.time | None, before: dt.time | None):
        self._after_t, self._before_t = after, before
        self._after = self._before = None
        self._placed = False

    def _place(self, t: dt.time | None, first: dt.datetime) -> dt.datetime | None:
        if t is None:
            return None
        d = dt.datetime.combine(first.date(), t)
        return d if d >= first else d + dt.timedelta(days=1)

    def contains(self, when: dt.datetime | None) -> bool:
        if self._after_t is None and self._before_t is None:
            return True
        if when is None:
            return False
        if not self._placed:
            self._after = self._place(self._after_t, when)
            self._before = self._place(self._before_t, when)
            self._placed = True
        if self._after is not None and when < self._after:
            return False
        return self._before is None or when <= self._before


def block_matches(header: str, body: list[str], args, ue_re, window: TimeWindow) -> bool:
    m = TS_RE.match(header)
    if not m:
        return False
    payload = m.group(3)

    if not window.contains(viavi_log_summary.parse_ts(m.group(1), m.group(2))):
        return False

    if args.event:
        if not payload.startswith("I: CMPI") or args.event.lower() not in payload.lower():
            return False

    full = "\n".join([header, *body])

    if ue_re and not ue_re.search(full):
        return False
    if args.pattern and not args.pattern.search(full):
        return False
    return True


def search(args) -> None:
    log_path = viavi_log_summary.resolve_log(args.path)
    # Match "UE Id:113" and "UE ID: 113" with an exact-id boundary; "UE ID: 0-32"
    # is a COMBINED range, not UE 0.
    ue_re = re.compile(rf"UE I[dD]:\s*{re.escape(args.ue)}(?![\d-])") if args.ue else None
    window = TimeWindow(args.after, args.before)

    blocks: list[str] = []
    header = None
    body: list[str] = []
    # (date, time) of the current GETSTATS record, whose stat rows repeat it.
    stats_stamp = None

    def flush():
        nonlocal header, body
        if header is not None and block_matches(header, body, args, ue_re, window):
            blocks.append("\n".join([header, *body]))
        header, body = None, []

    with viavi_log_summary.open_log(log_path) as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            m = TS_RE.match(line)
            if m:
                stamp, payload = (m.group(1), m.group(2)), m.group(3)
                is_record = viavi_log_summary.STATS_UE_RE.search(payload)
                if (stamp == stats_stamp and not is_record
                        and not payload.startswith(("I: ", "C: "))):
                    body.append(line)
                    continue
                flush()
                header, body = line, []
                stats_stamp = stamp if is_record else None
            elif header is not None:
                body.append(line)
        flush()

    if args.count:
        print(len(blocks))
        return

    shown = out_lines = 0
    for b in blocks:
        n = b.count("\n") + 2
        if shown > 0 and out_lines + n > args.max_lines:
            break
        print(b)
        print()
        out_lines += n
        shown += 1

    if shown < len(blocks):
        print(f"... {len(blocks) - shown} more matching block(s) not shown (stopped near "
              f"the {args.max_lines}-line cap). Narrow with --after/--before/--ue/--event.")


if __name__ == "__main__":
    args = parse_args()
    try:
        search(args)
    except viavi_log_summary.READ_ERRORS as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
