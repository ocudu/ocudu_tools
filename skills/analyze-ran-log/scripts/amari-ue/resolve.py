#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Resolve and inventory an Amarisoft UE-log input in one shot.

Locates the UE log (a UE log file → itself, e.g. ue.log or ue0.log; a directory
containing ue.log → its ue.log; a component dir `amarisoft-ue-N/` or a Retina
`test_gnb[...]` dir → the ue.log of the latest `YYYY-MM-DD_HH-MM-SS/` subdir),
confirms it is non-empty and looks like a UE log, lists the analysis artifacts
found, and warns when the input holds more than one UE component directory (so
the caller can scope to one rather than silently taking the latest). Prints a
compact verdict and exits non-zero if no usable UE log can be found.

This is resolution + inventory with a first-lines check, not a format preflight.

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

import ue_log_summary  # reuse resolve_ue_log() / is_ue_log_head()

PRIMARY = "ue.log"
ARTIFACTS = ("ue.log", "stdout.log", "amarisoft_ue.cfg")
# UE component directory names that can appear under a test_gnb[...] dir.
COMPONENT_GLOBS = ("amarisoft-ue-*",)


def _is_component_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in COMPONENT_GLOBS)


def find_components(root: Path) -> list[Path]:
    """Distinct UE component directories (each holding a ue.log) under root.

    For every ue.log beneath `root`, walk up to the nearest component-named
    ancestor. Returns [] when `root` is at or below a single run/component (no
    ambiguity to flag).
    """
    comps: set[Path] = set()
    for log in root.rglob(PRIMARY):
        if not log.is_file():
            continue
        for anc in log.parents:
            if anc == root or root not in anc.parents:
                break
            if _is_component_name(anc.name):
                comps.add(anc)
                break
    return sorted(comps)


def build_report(given: Path) -> dict:
    report: dict = {"input": str(given if not given.exists() else given.resolve())}
    if not given.exists():
        return {**report, "ok": False, "bail": f"not found: {given}"}

    root = given.resolve()
    components = find_components(root) if given.is_dir() else []

    try:
        primary = ue_log_summary.resolve_ue_log(str(given))
    except FileNotFoundError as e:
        return {**report, "ok": False, "bail": str(e)}

    run_dir = primary.parent
    report["run_dir"] = str(run_dir)
    if primary.name != PRIMARY:
        report["ue_log"] = primary.name
    if primary.stat().st_size == 0:
        return {**report, "ok": False, "bail": f"{primary.name} is empty (0 bytes)"}
    if not ue_log_summary.is_ue_log_head(primary):
        return {**report, "ok": False,
                "bail": f"{primary.name} does not look like an Amarisoft UE log "
                        "(no '#' header or HH:MM:SS.fff [LAYER] record in its first lines)"}

    present = [primary.name] + [a for a in ARTIFACTS[1:] if (run_dir / a).is_file()]
    report["present"] = present
    report["missing"] = [a for a in ARTIFACTS[1:] if not (run_dir / a).is_file()]
    cfg_path = run_dir / "amarisoft_ue.cfg"
    if cfg_path.is_file():
        cfg = ue_log_summary.parse_cfg(cfg_path)
        report["simulated_ues"] = cfg["ue_count"]
        report["n_ue_groups"] = cfg["n_ue_groups"]
    if len(components) > 1:
        report["components"] = [str(c.relative_to(root)) for c in components]
        report["resolved_component"] = next(
            (str(c.relative_to(root)) for c in components if c in run_dir.parents), None
        )
    report["ok"] = True
    return report


def render_text(r: dict) -> str:
    lines = [f"input:   {r['input']}"]
    if r.get("bail"):
        return "\n".join(lines + [f"verdict: BAIL — {r['bail']}"])
    lines.append(f"run dir: {r['run_dir']}")
    if r.get("ue_log"):
        lines.append(f"UE log:  {r['ue_log']}")
    present = ", ".join(r["present"]) if r["present"] else "none"
    line = f"present: {present}"
    if r["missing"]:
        line += f"   (missing: {', '.join(r['missing'])})"
    lines.append(line)
    if "simulated_ues" in r:
        groups_note = f" across {r['n_ue_groups']} imsi/ue_count blocks" if r["n_ue_groups"] > 1 else ""
        lines.append(f"simulated UEs (from amarisoft_ue.cfg): {r['simulated_ues']}{groups_note}")
    if r.get("components"):
        rc = r.get("resolved_component") or "the latest"
        lines.append(
            f"NOTE:    {len(r['components'])} UE components under the input "
            f"({', '.join(r['components'])}); resolved to {rc} — scope to another "
            f"explicitly if you meant a different one."
        )
    lines.append("verdict: OK")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("path", help="ue.log, a run dir, a component dir, or a test_gnb[...] dir")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    report = build_report(Path(args.path))
    print(json.dumps({**report, "verdict": "OK" if report.get("ok") else "BAIL"}, indent=2) if args.json else render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
