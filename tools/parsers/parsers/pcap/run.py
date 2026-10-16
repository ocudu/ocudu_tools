# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""The pcaps of an OCUDU run directory, and checks that tshark can dissect them."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .tshark import Tshark, TsharkError

PROTOCOLS = ("mac", "rlc", "f1ap", "e1ap", "ngap")
PCAP_NAMES = tuple(f"{proto}.pcap" for proto in PROTOCOLS)
# Prefixes of the pcap names of OCUDU applications, e.g. gnb_f1ap.pcap, preferred over those of other tools.
OCUDU_PREFIXES = ("gnb", "du", "cu", "cu_cp", "cu_up")
# Dissectors of the pcaps, one of which tshark binds on the first frame of each.
KNOWN_DISSECTORS = ("ngap", "f1ap", "e1ap", "mac-nr", "rlc-nr")
# Name of a Retina run directory, e.g. 2026-09-29_21-31-50, under the directory of its component, e.g. ocudu-du-1-1.
RUN_SUBDIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


def pcap_proto(path: str | os.PathLike[str]) -> str | None:
    """Protocol of a pcap by its file name, <proto>.pcap or .pcapng, or with a prefix, e.g. gnb_f1ap.pcap, or None."""
    p = Path(path)
    if p.suffix not in (".pcap", ".pcapng"):
        return None
    for proto in PROTOCOLS:
        if p.stem == proto or p.stem.endswith((f"_{proto}", f"-{proto}")):
            return proto
    return None


def find_pcaps(run_dir: str | os.PathLike[str]) -> dict[str, Path | None]:
    """The pcaps of a run directory by protocol, e.g. "mac", None for the missing ones. Of several pcaps of a protocol,
    <proto>.pcap, else one of an OCUDU prefix, else the first by name. A protocol missing from a Retina run directory,
    see RUN_SUBDIR_RE, is looked for in its parent, the directory of its component, e.g. the f1ap.pcap of a split DU.
    """
    p = Path(run_dir)
    if not p.is_dir():
        raise FileNotFoundError(f"not a directory: {p}")
    out: dict[str, Path | None] = dict.fromkeys(PROTOCOLS)
    dirs = [p, p.resolve().parent] if RUN_SUBDIR_RE.match(p.resolve().name) else [p]
    for d in dirs:
        for full in sorted(d.glob("*.pcap*"), key=_preference):
            proto = pcap_proto(full)
            if proto is not None and out[proto] is None and full.is_file():
                out[proto] = full
    return out


def _preference(path: Path) -> tuple[int, str]:
    if path.stem in PROTOCOLS:
        return 0, path.name
    prefix = path.stem.rpartition("_")[0] or path.stem.rpartition("-")[0]
    return (1 if prefix in OCUDU_PREFIXES else 2), path.name


def is_run_dir(path: str | os.PathLike[str]) -> bool:
    """Whether a directory holds the pcaps of at least two protocols."""
    p = Path(path)
    return p.is_dir() and sum(1 for f in find_pcaps(p).values() if f) >= 2


def resolve(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Classifies a path as a run directory or a single pcap.

    Returns "kind", "targets" (the pcaps to analyse) and "present" and "missing" protocols for a run directory or
    "siblings" for a single pcap, or only "bail" with the reason when the path is neither.
    """
    path = Path(path)
    if not path.exists():
        return {"bail": f"not found: {path}"}
    if path.is_dir():
        pcaps = find_pcaps(path)
        present = [proto for proto, p in pcaps.items() if p]
        if len(present) < 2:
            found = ", ".join(present) if present else "none"
            return {"bail": f"not a run directory (need pcaps of ≥2 of {', '.join(PROTOCOLS)}); found: {found}"}
        return {
            "kind": "run directory",
            "targets": [pcaps[proto] for proto in present],
            "present": present,
            "missing": [proto for proto, p in pcaps.items() if not p],
        }
    pcaps = find_pcaps(path.parent)
    siblings = [proto for proto, p in pcaps.items() if p and p.resolve() != path.resolve()]
    return {"kind": "single pcap", "targets": [path], "siblings": siblings}


def check_pcap(tshark: Tshark, pcap: str | os.PathLike[str]) -> dict[str, Any]:
    """Checks that a pcap is a non-empty Upper-PDU capture with a known dissector on its first frame.

    Returns "file", "path" and "ok", with "reason" when not ok, and "frame_protocols" and "dissector" once read.
    """
    pcap = Path(pcap)
    out: dict[str, Any] = {"file": pcap.name, "path": str(pcap)}
    try:
        if pcap.stat().st_size == 0:
            return {**out, "ok": False, "reason": "empty (0 bytes)"}
    except OSError as e:
        return {**out, "ok": False, "reason": f"cannot stat: {e}"}
    try:
        rows = tshark.run(["-r", str(tshark.stage(pcap)), "-c", "1", "-T", "fields", "-e", "frame.protocols"])
    except TsharkError as e:
        return {**out, "ok": False, "reason": f"tshark read failed: {e}"}
    protos = rows[0] if rows else ""
    if not protos:
        return {**out, "ok": False, "reason": "empty (0 frames)"}

    # The dissector can sit mid-chain, e.g. "exported_pdu:udp:mac-nr:nr-rrc" for a MAC PDU carrying RRC.
    tokens = protos.split(":")
    found = [d for d in KNOWN_DISSECTORS if d in tokens]
    out.update(frame_protocols=protos, dissector=found[0] if found else tokens[-1])
    if "exported_pdu" not in tokens:
        return {**out, "ok": False, "reason": f"not Upper-PDU (frame.protocols={protos})"}
    if not found:
        return {**out, "ok": False, "reason": f"no 3GPP dissector bound (frame.protocols={protos})"}
    return {**out, "ok": True}
