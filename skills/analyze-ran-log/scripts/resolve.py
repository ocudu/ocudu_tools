#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Classify a RAN test artifact and delegate to its per-type resolve/preflight.

This is the single entrypoint for the analyze-ran-log skill. It classifies
the input into a kind and then runs that kind's resolve script as a subprocess,
so each per-type script keeps its own directory on sys.path for its sibling
imports (e.g. resolve.py importing ocudu_log_summary):

    kind        marker                                  delegate
    ----------  --------------------------------------  ----------------------------
    pcap        *.pcap/*.pcapng, or a dir of them       scripts/pcap/resolve.py
    ocudu       gnb.log / OCUDU component or run        scripts/ocudu/resolve.py
    amari-ue    ue.log / Amarisoft UE component/run     scripts/amari-ue/resolve.py
    viavi       *_Command_Log*.txt/.zip (or a dir)      scripts/viavi/resolve.py
    correlate   a run dir spanning ≥2 RAN components     scripts/correlate/resolve.py

The kind line and a `-> read references/<type>/` pointer are printed first, then
the delegate's own output (inventory/validation + its `verdict:` line). The exit
code is the delegate's. A whole `test_gnb[...]` run that spans several RAN
application components (gNB + UE + 5GC) resolves to `correlate` for cross-artifact
work; a single artifact or single-component dir resolves to its own type. Pass
`--type` to force a kind.

Usage:
    resolve.py <artifact-or-dir> [--type pcap|ocudu|amari-ue|viavi|correlate] [passthrough args...]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "_lib"))

from parsers.pcap.run import COMPONENT_DIR_RE, RUN_SUBDIR_RE  # noqa: E402

# kind -> (subdir, script) it delegates to.
DELEGATE = {
    "pcap": ("pcap", "resolve.py"),
    "ocudu": ("ocudu", "resolve.py"),
    "amari-ue": ("amari-ue", "resolve.py"),
    "viavi": ("viavi", "resolve.py"),
    "correlate": ("correlate", "resolve.py"),
}
KINDS = tuple(DELEGATE)

# OCUDU pcap names, <proto>.pcap or with a prefix, e.g. gnb_f1ap.pcap.
PCAP_GLOBS = ("*mac.pcap", "*rlc.pcap", "*f1ap.pcap", "*e1ap.pcap", "*ngap.pcap")
# OCUDU app log basenames that mark an ocudu artifact.
GNB_LOGS = ("gnb.log", "du.log", "cu.log", "cu_cp.log", "cu_up.log")
UE_LOG = "ue.log"
# 5GC/MME log basenames (Amarisoft core).
CORE_LOGS = ("mme.log", "amf.log")
# VIAVI RU-simulator command log: "<YYMMDD_HHMMSS>_Command_Log<NNN>.{txt,zip}".
VIAVI_GLOB = "*Command_Log*"
VIAVI_SUFFIXES = (".txt", ".zip")
# First-lines signature, used to confirm a bare *.txt/*.zip is a VIAVI log.
VIAVI_SIGNATURE = re.compile(r"(C: RSET 0x|I: CMPI |^\d{2}/\d{2}/\d{2} [\d:]+ RSET\b)", re.M)


def _has_under(root: Path, name: str) -> bool:
    """True if `name`, a name or glob, exists at `root` or anywhere beneath it (bounded by first hit)."""
    if (root / name).is_file():
        return True
    return next(root.rglob(name), None) is not None


def _has_any_under(root: Path, names) -> bool:
    return any(_has_under(root, n) for n in names)


def _looks_viavi_file(path: Path) -> bool:
    """True if `path` is a VIAVI command log — by name, else by content signature."""
    if path.suffix.lower() not in VIAVI_SUFFIXES:
        return False
    if "Command_Log" in path.name:
        return True
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                member = (names or zf.namelist())[0]
                with zf.open(member) as fh:
                    head = fh.read(8192).decode("utf-8", "replace")
        else:
            with open(path, "rb") as fh:
                head = fh.read(8192).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile, IndexError):
        return False
    return bool(VIAVI_SIGNATURE.search(head))


def _has_viavi_under(root: Path) -> bool:
    """True if a *Command_Log*.{txt,zip} exists at or below `root`."""
    for suf in VIAVI_SUFFIXES:
        if next((p for p in root.rglob(VIAVI_GLOB + suf) if p.is_file()), None):
            return True
    return False


def _is_component_dir(path: Path) -> bool:
    """True if `path` is an OCUDU component dir of a Retina run: an app log in a YYYY-MM-DD_HH-MM-SS subdir."""
    if not COMPONENT_DIR_RE.match(path.resolve().name):
        return False
    return any(RUN_SUBDIR_RE.match(d.name) and any((d / n).is_file() for n in GNB_LOGS)
               for d in path.iterdir() if d.is_dir())


def classify(path: Path) -> tuple[str | None, str]:
    """Return (kind, reason). kind is None when the input is ambiguous/unknown."""
    if path.is_file():
        suf = path.suffix.lower()
        if suf in (".pcap", ".pcapng"):
            return "pcap", f"{path.name} is a packet capture"
        if path.name == UE_LOG:
            return "amari-ue", "ue.log is an Amarisoft UE log"
        if path.name in GNB_LOGS:
            return "ocudu", f"{path.name} is an OCUDU app log"
        if _looks_viavi_file(path):
            return "viavi", f"{path.name} is a VIAVI command log"
        return None, f"unrecognized file: {path.name}"

    if not path.is_dir():
        return None, f"not a file or directory: {path}"

    # A directory that directly holds the primary log is unambiguous — point at it.
    if any((path / n).is_file() for n in GNB_LOGS):
        return "ocudu", "directory contains an OCUDU app log"
    if (path / UE_LOG).is_file():
        return "amari-ue", "directory contains ue.log"
    # A component dir, e.g. the ocudu-du-1-1 of a split DU, holds pcaps beside the run subdir with its app log.
    if not _is_component_dir(path) and any(p.is_file() for g in PCAP_GLOBS for p in path.glob(g)):
        return "pcap", "directory contains Upper-PDU pcaps"
    direct_viavi = any("Command_Log" in p.name and p.is_file()
                       for suf in VIAVI_SUFFIXES for p in path.glob(VIAVI_GLOB + suf))
    # A Retina test dir keeps the tester log at its root *beside* component dirs, so
    # holding one is only unambiguous when no other RAN component lives below.
    other_components = (_has_any_under(path, GNB_LOGS) or _has_under(path, UE_LOG)
                        or _has_any_under(path, CORE_LOGS))
    if direct_viavi and not other_components:
        return "viavi", "directory contains a VIAVI command log"

    # Component / test directory: detect by what lives beneath it.
    has_gnb = _has_any_under(path, GNB_LOGS)
    has_ue = _has_under(path, UE_LOG)
    has_core = _has_any_under(path, CORE_LOGS)
    has_pcap = _has_any_under(path, PCAP_GLOBS)
    has_viavi = _has_viavi_under(path)
    # A directory spanning ≥2 RAN application components is a whole-run directory
    # → cross-artifact correlation. Count each OCUDU *role* separately: a split
    # CU-CP + CU-UP + DU deployment is several components that need joining, even
    # though they are all "the OCUDU app".
    ocudu_roles = sorted({n[:-4] for n in GNB_LOGS if _has_under(path, n)})
    apps: list[str] = []
    if ocudu_roles:
        apps.append("OCUDU " + "+".join(ocudu_roles))
    if has_ue:
        apps.append("Amarisoft UE")
    if has_core:
        apps.append("5GC")
    if has_viavi:
        apps.append("VIAVI tester")
    n_components = len(ocudu_roles) + has_ue + has_core + has_viavi
    if n_components >= 2:
        return "correlate", f"directory spans multiple RAN components ({', '.join(apps)})"
    if has_gnb:
        return "ocudu", "OCUDU app log found below the directory"
    if has_ue:
        return "amari-ue", "ue.log found below the directory"
    if has_pcap:
        return "pcap", "Upper-PDU pcaps found below the directory"
    if has_viavi:
        return "viavi", "VIAVI command log found below the directory"
    return None, "no gnb.log / ue.log / *.pcap / command log found"


def main(argv: list[str]) -> int:
    args = argv[1:]
    forced: str | None = None
    if "--type" in args:
        i = args.index("--type")
        try:
            forced = args[i + 1]
        except IndexError:
            print("error: --type requires a value", file=sys.stderr)
            return 2
        if forced not in KINDS:
            print(f"error: --type must be one of {', '.join(KINDS)}", file=sys.stderr)
            return 2
        del args[i : i + 2]

    if not args:
        print(
            "usage: resolve.py <artifact-or-dir> "
            "[--type pcap|ocudu|amari-ue|viavi|correlate] [passthrough args...]",
            file=sys.stderr,
        )
        return 2

    target = Path(args[0]).expanduser()
    passthrough = args[1:]

    if forced:
        kind, reason = forced, "forced via --type"
    else:
        kind, reason = classify(target)

    if kind is None:
        print(f"kind: UNKNOWN ({reason})")
        print("verdict: BAIL")
        return 1

    print(f"kind: {kind} ({reason})")
    print(f"-> read references/{kind}/")
    print(f"-> conventions: references/{kind}/conventions.md")
    print()
    sys.stdout.flush()  # ensure the headline precedes the delegate's output when piped

    subdir, script = DELEGATE[kind]
    delegate = SCRIPT_DIR / subdir / script
    if not delegate.is_file():
        print(f"error: delegate script missing: {delegate}", file=sys.stderr)
        print("verdict: BAIL")
        return 1

    proc = subprocess.run(
        [sys.executable, str(delegate), str(target), *passthrough],
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
