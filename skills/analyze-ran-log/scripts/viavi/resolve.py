#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Resolve and inventory a VIAVI command-log input in one shot.

Locates the command log (a *_Command_Log*.txt/.zip file → itself; a directory
containing one → the newest basename, preferring its .txt over its identical .zip),
confirms it is non-empty and carries the VIAVI signature in its first lines, and
prints a compact verdict. Exits non-zero (BAIL) if no usable command log is
found or the content does not look like a VIAVI log.

The .txt and .zip hold identical content — the .zip is a compressed copy, not a
distinct artifact — so a directory resolves to the .txt and a .zip is read in
place. Like the other per-type resolvers this is resolution + inventory, not a
deep format preflight.

Usage:
    resolve.py <path>
    resolve.py <path> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import viavi_log_summary  # reuse resolve_log() / is_viavi_head()


def build_report(given: Path) -> dict:
    report: dict = {"input": str(given if not given.exists() else given.resolve())}
    if not given.exists():
        return {**report, "ok": False, "bail": f"not found: {given}"}

    try:
        log = viavi_log_summary.resolve_log(str(given))
    except FileNotFoundError as e:
        return {**report, "ok": False, "bail": str(e)}

    report["log"] = str(log)
    report["format"] = "zip" if log.suffix.lower() == ".zip" else "txt"
    if log.stat().st_size == 0:
        return {**report, "ok": False, "bail": f"{log.name} is empty (0 bytes)"}
    try:
        with viavi_log_summary.open_log(log) as f:
            f.read(1)
    except viavi_log_summary.READ_ERRORS as e:
        return {**report, "ok": False, "bail": f"cannot read: {e}"}
    if not viavi_log_summary.is_viavi_head(log):
        return {**report, "ok": False,
                "bail": f"{log.name} does not look like a VIAVI command log "
                        "(no RSET/CMPI/TMAE signature in its first lines)"}

    report["size_mb"] = round(log.stat().st_size / 1e6, 1)
    report["ok"] = True
    return report


def render_text(r: dict) -> str:
    lines = [f"input:   {r['input']}"]
    if r.get("bail"):
        return "\n".join(lines + [f"verdict: BAIL — {r['bail']}"])
    lines.append(f"log:     {r['log']}  ({r['format']}, {r['size_mb']} MB)")
    if r["format"] == "zip":
        lines.append("NOTE:    read from .zip (identical content to the .txt)")
    lines.append("verdict: OK")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("path", help="a *_Command_Log*.txt/.zip file or a directory")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    report = build_report(Path(args.path).expanduser())
    print(json.dumps({**report, "verdict": "OK" if report.get("ok") else "BAIL"}, indent=2) if args.json else render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
