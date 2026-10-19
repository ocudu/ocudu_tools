#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Resolve and inventory an OCUDU app-log input in one shot.

Handles every OCUDU app role — `gnb.log`, `du.log`, `cu.log`, `cu_cp.log`,
`cu_up.log` — since they share one log format and differ only by name.

Locates the run directory (a log file → its parent; a directory containing one →
itself; a component dir `ocudu-{gnb,du,cu,cu-cp,cu-up}-*/` or a Retina
`test_gnb[...]` dir → the latest `YYYY-MM-DD_HH-MM-SS/` subdir that has one),
confirms the log is present and non-empty, lists the analysis artifacts found, and warns
when a test directory holds more than one OCUDU app component (so the caller can
scope to one rather than silently taking the latest). Prints a compact verdict
and exits non-zero if no usable gnb.log can be found.

Unlike pcaps, gNB logs are plain text with nothing to validate — this is
resolution + inventory only, not a format preflight.

Usage:
    resolve.py <path>
    resolve.py <path> --json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))

from parsers.log import run  # noqa: E402

# All five OCUDU app log names resolve as kind `ocudu` -- same format, different
# role. Sourced from parsers.log.run so the two can't drift apart.
OCUDU_LOG_NAMES = run.LOG_NAMES
# Globs of the files beside the app log. Split runs name the stdout of each app, e.g. stdout_du.log.
SIDECARS = ("stdout*.log", "metrics.json")
# OCUDU app component directory names that can appear under a test_gnb[...] dir.
COMPONENT_GLOBS = ("ocudu-gnb-*", "ocudu-cu-*", "ocudu-cu-cp-*", "ocudu-cu-up-*", "ocudu-du-*")


def _is_component_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in COMPONENT_GLOBS)


def find_components(root: Path) -> list[str]:
    """Names of distinct OCUDU app components (each holding a gnb.log) under root.

    For every gnb.log beneath `root`, walk up to the nearest component-named
    ancestor; the set of those is what a test directory exposes. Returns [] when
    `root` is at or below a single run/component (no ambiguity to flag).
    """
    comps: set[str] = set()
    for name in OCUDU_LOG_NAMES:
        for log in root.rglob(name):
            if not log.is_file():
                continue
            for anc in log.parents:
                if _is_component_name(anc.name):
                    comps.add(anc.name)
                    break
                if anc == root:
                    break
    return sorted(comps)


def build_report(given: Path) -> dict:
    report: dict = {"input": str(given if not given.exists() else given.resolve())}
    if not given.exists():
        return {**report, "ok": False, "bail": f"not found: {given}"}

    components = find_components(given.resolve()) if given.is_dir() else []

    try:
        run_dir = run.resolve_run_dir(str(given))
    except FileNotFoundError as e:
        return {**report, "ok": False, "bail": str(e)}

    report["run_dir"] = str(run_dir)
    primary = run.primary_log_in(run_dir)
    if primary is None:
        return {
            **report, "ok": False,
            "bail": f"no OCUDU app log ({', '.join(OCUDU_LOG_NAMES)}) in {run_dir}",
        }
    if primary.stat().st_size == 0:
        return {**report, "ok": False, "bail": f"{primary.name} is empty (0 bytes)"}

    report["primary_log"] = primary.name
    report["role"] = primary.stem.replace("_", "-")
    report["present"] = []
    report["missing"] = []
    for glob in (primary.name, *SIDECARS, run.CONFIG_GLOB):
        found = sorted(f.name for f in run_dir.glob(glob) if f.is_file())
        if found:
            report["present"] += found
        else:
            report["missing"].append(glob)
    if len(components) > 1:
        report["components"] = components
        report["resolved_component"] = next(
            (c for c in components if c in str(run_dir)), None
        )
    report["ok"] = True
    return report


def render_text(r: dict) -> str:
    lines = [f"input:   {r['input']}"]
    if r.get("bail"):
        return "\n".join(lines + [f"verdict: BAIL — {r['bail']}"])
    lines.append(f"run dir: {r['run_dir']}")
    if r.get("primary_log"):
        lines.append(f"role:    {r['role']}  (log: {r['primary_log']})")
    present = ", ".join(r["present"]) if r["present"] else "none"
    line = f"present: {present}"
    if r["missing"]:
        line += f"   (missing: {', '.join(r['missing'])})"
    lines.append(line)
    if r.get("components"):
        rc = r.get("resolved_component") or "the latest"
        lines.append(
            f"NOTE:    {len(r['components'])} OCUDU app components under the input "
            f"({', '.join(r['components'])}); resolved to {rc} — scope to another "
            f"explicitly if you meant a different one."
        )
    lines.append("verdict: OK")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("path", help="an OCUDU app log (gnb/du/cu/cu_cp/cu_up), a run dir, a component dir, or a test_gnb[...] dir")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    report = build_report(Path(args.path))
    print(json.dumps({**report, "verdict": "OK" if report.get("ok") else "BAIL"}, indent=2) if args.json else render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
