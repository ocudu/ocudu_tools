#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
resolve.py — resolve/inventory a multi-component OCUDU run for correlation.

The `correlate` kind's resolver (the analyze-ran-log dispatcher delegates here
when an input spans several RAN application components). Enumerates the
components of a `test_gnb[...]` directory (OCUDU gNB/DU/CU, Amarisoft UE,
Amarisoft 5GC), resolves each component's latest run subdir, lists the artifacts
present, maps each to the analyze-ran-log type that analyses it, parses
testbed.json (component -> IP:port), and reports the per-source clock anchors
needed for cross-correlation. Ends with a `verdict:` line.

Usage:
    python3 resolve.py <test-dir | component-dir | run-dir> [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import utils

# Component dir prefix -> (role, analyze-ran-log type for its primary log).
COMPONENT_ROLES = [
    ("ocudu-cu-cp", "cu-cp", "ocudu"),
    ("ocudu-cu-up", "cu-up", "ocudu"),
    ("ocudu-cu", "cu", "ocudu"),
    ("ocudu-du", "du", "ocudu"),
    ("ocudu-gnb", "gnb", "ocudu"),
    ("ocudu-odu", "odu", "ocudu"),
    ("ocudu-ocu", "ocu", "ocudu"),
    ("amarisoft-ue", "ue", "amari-ue"),
    ("amarisoft-5gc", "5gc", "(light-touch; future amari-5gc type)"),
    ("amarisoft-mme", "5gc", "(light-touch; future amari-5gc type)"),
]

RUN_SUBDIR_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

# The VIAVI tester writes a loose `*_Command_Log*.txt`/`.zip` rather than living in
# an `ocudu-*`/`amarisoft-*` component dir, so it needs its own discovery.
VIAVI_GLOB = "*Command_Log*"
VIAVI_SUFFIXES = (".txt", ".zip")


def find_viavi_logs(root: Path) -> list[Path]:
    """VIAVI command logs at or below root, newest last, .txt preferred over .zip."""
    hits: list[Path] = []
    for suf in VIAVI_SUFFIXES:
        hits += [p for p in root.rglob(VIAVI_GLOB + suf) if p.is_file()]
    # Drop a .zip whose identical .txt sibling is also present.
    txt_stems = {p.with_suffix("").name for p in hits if p.suffix == ".txt"}
    hits = [p for p in hits if p.suffix != ".zip" or p.with_suffix("").name not in txt_stems]
    return sorted(hits, key=lambda p: (p.stat().st_mtime, p.name))

# Primary OCUDU app-log filenames, in preference order.
OCUDU_LOG_NAMES = ("gnb.log", "du.log", "cu.log", "cu_cp.log", "cu_up.log")
# OCUDU pcap names, <proto>.pcap or with a prefix, e.g. gnb_f1ap.pcap.
PCAP_GLOBS = ("*ngap.pcap", "*f1ap.pcap", "*e1ap.pcap", "*mac.pcap", "*rlc.pcap")


def classify(component_dir_name: str):
    for prefix, role, atype in COMPONENT_ROLES:
        if component_dir_name.startswith(prefix):
            return role, atype
    return None, None


def latest_run_subdir(component_dir: Path) -> Path:
    """Return the latest YYYY-MM-DD_HH-MM-SS subdir, or the component dir itself if flat."""
    subs = sorted(
        (d for d in component_dir.iterdir() if d.is_dir() and RUN_SUBDIR_RE.match(d.name)),
        key=lambda d: d.name,
    )
    return subs[-1] if subs else component_dir


def resolve_test_dir(path: Path) -> tuple[Path, list[Path]]:
    """Return (test_dir, component_dirs). Accepts a test dir, a component dir, or a run dir."""
    path = path.resolve()

    def component_children(d: Path) -> list[Path]:
        return sorted(
            c for c in d.iterdir()
            if c.is_dir() and classify(c.name)[0] is not None
        )

    if path.is_dir():
        comps = component_children(path)
        if comps:
            return path, comps
        # A component dir given directly: report the parent as the test dir but
        # scope to just this component (we don't pull in sibling components when
        # the user pointed at one specifically).
        if classify(path.name)[0] is not None and path.parent.is_dir():
            return path.parent, [path]
        # A run subdir inside a component dir?
        if RUN_SUBDIR_RE.match(path.name) and classify(path.parent.name)[0] is not None:
            return path.parent.parent, [path.parent]
        # No component dirs, but a VIAVI command log below → still a valid run to
        # correlate (tester + whatever else is here).
        if find_viavi_logs(path):
            return path, []
    raise SystemExit(
        f"error: no OCUDU/Amarisoft component dirs and no VIAVI command log under {path}"
    )


def inventory_component(comp_dir: Path, run_dir: Path | None = None) -> dict:
    """Inventory of a component, of its run subdir run_dir, by default its latest one."""
    role, atype = classify(comp_dir.name)
    run_dir = run_dir or latest_run_subdir(comp_dir)
    info: dict = {
        "component": comp_dir.name,
        "role": role,
        "type": atype,
        "run_dir": str(run_dir),
        "logs": [],
        "pcaps": [],
        "configs": [],
        "metrics": False,
        "clock_anchor": None,
    }

    # OCUDU app log
    if role in ("gnb", "du", "cu", "cu-cp", "cu-up", "odu", "ocu"):
        for name in OCUDU_LOG_NAMES:
            f = run_dir / name
            if f.is_file():
                info["logs"].append(name)
        primary = next((run_dir / n for n in OCUDU_LOG_NAMES if (run_dir / n).is_file()), None)
        if primary:
            info["clock_anchor"] = {"type": "gnb-utc", "first_event": utils.first_gnb_event_ts(primary)}
        info["configs"] += sorted(c.name for c in run_dir.glob("ocudu_*.yml"))
        for g in PCAP_GLOBS:
            info["pcaps"] += sorted(p.name for p in run_dir.glob(g) if p.is_file())
        # A split DU writes its f1ap.pcap in the component directory rather than in its run directory.
        if run_dir != comp_dir:
            for g in PCAP_GLOBS:
                info["pcaps"] += sorted(f"../{p.name}" for p in comp_dir.glob(g)
                                        if p.is_file() and p.name not in info["pcaps"])
    elif role == "ue":
        if (run_dir / "ue.log").is_file():
            info["logs"].append("ue.log")
            info["clock_anchor"] = {"type": "amari-utc",
                                    "started_on": utils.started_on_date(run_dir / "ue.log")}
        if (run_dir / "amarisoft_ue.cfg").is_file():
            info["configs"].append("amarisoft_ue.cfg")
    elif role == "5gc":
        for name in ("mme.log", "amf.log", "open5gs.log"):
            if (run_dir / name).is_file():
                info["logs"].append(name)
        if info["logs"]:
            info["clock_anchor"] = {"type": "amari-utc",
                                    "started_on": utils.started_on_date(run_dir / info["logs"][0])}
        for c in ("amarisoft_mme.cfg", "amarisoft_amf.cfg"):
            if (run_dir / c).is_file():
                info["configs"].append(c)

    if (run_dir / "metrics.json").is_file():
        info["metrics"] = True
    return info


def viavi_pseudo_component(log: Path, test_dir: Path) -> dict:
    """Inventory entry for a VIAVI command log (no component dir of its own)."""
    return {
        "component": str(log.relative_to(test_dir)) if test_dir in log.parents else log.name,
        "role": "tester",
        "type": "viavi",
        "run_dir": str(log.parent),
        "logs": [log.name],
        "pcaps": [],
        "configs": [],
        "metrics": False,
        # Tester-local timestamps: DD/MM/YY HH:MM:SS:mmm, and the host may differ
        # from the gNB's, so this anchor is NOT comparable without measuring Δ.
        "clock_anchor": {"type": "viavi-tester-local", "offset_measured": False},
    }


def build_inventory(path_str: str) -> dict:
    test_dir, comp_dirs = resolve_test_dir(Path(path_str))
    path = Path(path_str).resolve()
    # A run subdir given directly is inventoried, not the latest run of its component.
    given_run = path if RUN_SUBDIR_RE.match(path.name) and comp_dirs == [path.parent] else None
    comps = [inventory_component(c, given_run) for c in comp_dirs]
    comps += [viavi_pseudo_component(v, test_dir) for v in find_viavi_logs(test_dir)]
    testbed = {}
    tb = test_dir / "testbed.json"
    if tb.is_file():
        testbed = utils.parse_testbed(tb)
    return {"test_dir": str(test_dir), "testbed": testbed, "components": comps}


def render_text(inv: dict) -> str:
    lines = [f"Test directory : {inv['test_dir']}", ""]
    lines.append("Components:")
    for c in inv["components"]:
        lines.append(f"  [{c['role']}] {c['component']}  (analyze-ran-log type: {c['type']})")
        rd = Path(c["run_dir"]).name
        lines.append(f"        run subdir : {rd}")
        if c["logs"]:
            lines.append(f"        logs       : {', '.join(c['logs'])}")
        if c["configs"]:
            lines.append(f"        configs    : {', '.join(c['configs'])}")
        if c["pcaps"]:
            lines.append(f"        pcaps      : {', '.join(c['pcaps'])}  (analyze-ran-log type: pcap)")
        if c["metrics"]:
            lines.append("        metrics    : metrics.json")
        ca = c["clock_anchor"]
        if ca and ca.get("type") == "gnb-utc" and ca.get("first_event"):
            lines.append(f"        clock      : first event {ca['first_event']} (UTC)")
        elif ca and ca.get("type") == "amari-utc" and ca.get("started_on"):
            lines.append(f"        clock      : # Started on {ca['started_on']} (UTC)")
    if inv["testbed"]:
        lines.append("")
        lines.append("Testbed map (component -> address:port):")
        # Collapse large families of identical-prefix components into one range
        # line to keep the output token-cheap. NOTE: this is a count of testbed
        # component *slots* (containers/processes), NOT the number of simulated
        # UEs — a single amarisoft-ue-N container can simulate many UEs via its
        # amarisoft_ue.cfg. For the real simulated-UE count, resolve that
        # component with the amari-ue type (its resolve.py reports it from cfg).
        ue_items = [(n, ni) for n, ni in inv["testbed"].items() if n.startswith("amarisoft-ue-")]
        other = [(n, ni) for n, ni in inv["testbed"].items() if not n.startswith("amarisoft-ue-")]
        for name, ni in other:
            lines.append(f"  {name:<22} {ni['address']}:{ni['port']}")
        if ue_items:
            ports = sorted(ni["port"] for _, ni in ue_items)
            addr = ue_items[0][1]["address"]
            if len(ue_items) == 1:
                n, ni = ue_items[0]
                lines.append(f"  {n:<22} {ni['address']}:{ni['port']}")
            else:
                lines.append(f"  amarisoft-ue-1..{len(ue_items):<10} {addr}:{ports[0]}-{ports[-1]} "
                             f"({len(ue_items)} testbed slots — NOT simulated-UE count, "
                             f"see amari-ue resolve for that)")
    lines.append("")
    lines.append("Clock note: same-process sources (a component's log and the pcaps it")
    lines.append("wrote) share a clock, Δ≈0. Off-host sources (VIAVI tester, a remote 5GC, a")
    lines.append("UE sim on another box) may be genuinely offset — do NOT compare wall-clocks")
    lines.append("until Δ is known. capinfos/tshark DISPLAY in local TZ — use raw")
    lines.append("frame.time_epoch. PHY (SFN.slot, RNTI) is the exact, clock-independent key.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="test dir, component dir, or run dir")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if not Path(args.path).exists():
        print(f"error: not found: {args.path}", file=sys.stderr)
        print("verdict: BAIL")
        return 1

    try:
        inv = build_inventory(args.path)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        print("verdict: BAIL")
        return 1
    if args.json:
        # Mirror the text render's verdict line so both modes honour the
        # documented "bail if the verdict is not OK" contract.
        print(json.dumps({**inv, "verdict": "OK"}, indent=2))
    else:
        print(render_text(inv))
        print("\nverdict: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
