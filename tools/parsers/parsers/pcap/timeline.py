# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Time-ordered events of the pcaps of an OCUDU run, each with its UE identifiers and a one-line summary."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from ..ran.rnti import normalize as normalize_rnti
from .run import find_pcaps
from .tshark import Tshark, TsharkError
from .values import epoch_to_iso, first

logger = logging.getLogger("parsers")

PROTOCOLS = ("ngap", "f1ap", "e1ap", "mac", "rlc")

# The mac-nr.rnti-type of a C-RNTI.
_C_RNTI_TYPE = "3"
# Fields of the events of each protocol.
EVENT_FIELDS: dict[str, list[str]] = {
    "ngap": ["frame.number", "frame.time_epoch", "ngap.procedureCode", "ngap.RAN_UE_NGAP_ID", "ngap.AMF_UE_NGAP_ID"],
    "f1ap": ["frame.number", "frame.time_epoch", "f1ap.procedureCode", "f1ap.GNB_DU_UE_F1AP_ID", "f1ap.GNB_CU_UE_F1AP_ID"],
    "e1ap": [
        "frame.number", "frame.time_epoch", "e1ap.procedureCode", "e1ap.GNB_CU_CP_UE_E1AP_ID", "e1ap.GNB_CU_UP_UE_E1AP_ID",
    ],
    "mac": ["frame.number", "frame.time_epoch", "mac-nr.rnti", "mac-nr.direction", "mac-nr.rnti-type"],
    "rlc": ["frame.number", "frame.time_epoch", "rlc-nr.ueid", "rlc-nr.bearer-type", "rlc-nr.bearer-id"],
}


def events(tshark: Tshark, pcap: str | os.PathLike[str], proto: str) -> list[dict[str, Any]]:
    """The events of a pcap of a protocol in PROTOCOLS, in file order.

    Each has "epoch", "iso", "file" (the protocol), "frame", "ue_ids" and "summary".
    """
    out: list[dict[str, Any]] = []
    fields = EVENT_FIELDS[proto]
    # The fields are part of the tag, since they depend on the protocol and this code.
    tag = f"correlate-{proto}-" + hashlib.sha256("\0".join(fields).encode()).hexdigest()[:16]
    for frame, epoch, *rest in tshark.iter_fields(pcap, fields, tag=tag):
        try:
            ts = float(epoch)
        except ValueError:
            continue
        if proto in ("ngap", "f1ap", "e1ap"):
            ue_ids = [v for v in map(first, rest[1:]) if v]
            summary = f"procCode={rest[0]}"
        elif proto == "mac":
            # Only C-RNTIs are of UEs, not the SI-, P- and RA-RNTIs of common channels.
            ue_ids = [rest[0]] if rest[0] and rest[2] == _C_RNTI_TYPE else []
            dir_label = {"0": "UL", "1": "DL"}.get(rest[1], rest[1] or "?")
            summary = f"rnti={rest[0] or '-'} dir={dir_label}"
        else:
            ue_ids = [rest[0]] if rest[0] else []
            summary = f"ueid={rest[0] or '-'} bearer={rest[1] or '?'}/{rest[2] or '?'}"
        out.append({
            "epoch": ts,
            "iso": epoch_to_iso(ts),
            "file": proto,
            "frame": int(frame) if frame else None,
            "ue_ids": ue_ids,
            "summary": summary,
        })
    return out


def run_events(
    tshark: Tshark,
    run_dir: str | os.PathLike[str],
    protocols: list[str] | tuple[str, ...] = PROTOCOLS,
    *,
    around: float | None = None,
    window_ms: int = 2000,
    ue: str | None = None,
) -> list[dict[str, Any]]:
    """The events of the pcaps of a run directory in time order, see events().

    around keeps the events within window_ms centred on an epoch, and ue the events with that UE identifier: a protocol
    UE id as the pcap has it, or an RNTI in decimal or 0x hexadecimal, which MAC events match however they print it.
    Missing pcaps are logged as one warning, and unreadable pcaps and pcaps whose time spans do not overlap as warnings.
    Raises TsharkError when no pcap found is readable.
    """
    run_dir = Path(run_dir)
    pcaps = find_pcaps(run_dir)
    spans: list[tuple[str, float, float]] = []
    out: list[dict[str, Any]] = []
    failures: list[str] = []
    missing = [proto for proto in protocols if pcaps.get(proto) is None]
    if missing:
        logger.warning("no pcap of %s in %s", ", ".join(missing), run_dir)
    for proto in protocols:
        pcap = pcaps.get(proto)
        if pcap is None:
            continue
        try:
            ev = events(tshark, pcap, proto)
        except TsharkError as e:
            logger.warning("tshark failed for %s: %s", proto, e)
            failures.append(f"{proto}: {e}")
            continue
        if ev:
            epochs = [e["epoch"] for e in ev]
            spans.append((proto, min(epochs), max(epochs)))
        out.extend(ev)
    if failures and len(failures) == len(protocols) - len(missing):
        raise TsharkError("no pcap read; " + "; ".join(failures))

    # Captures starting or stopping a few seconds apart are normal, only disjoint ones prevent correlation.
    if len(spans) > 1 and max(s[1] for s in spans) > min(s[2] for s in spans):
        ranges = ", ".join(f"{p}={epoch_to_iso(lo)}..{epoch_to_iso(hi)}" for p, lo, hi in spans)
        logger.warning("pcap time ranges do not all overlap — cross-pcap correlation may be unreliable: %s", ranges)

    if around is not None:
        half = window_ms / 2000.0
        out = [e for e in out if around - half <= e["epoch"] <= around + half]
    if ue:
        out = [e for e in out if _has_ue(e, ue)]
    out.sort(key=lambda e: e["epoch"])
    return out


def _has_ue(event: dict[str, Any], ue: str) -> bool:
    if ue in event["ue_ids"]:
        return True
    if event["file"] == "mac" and (rnti := normalize_rnti(ue)) is not None:
        return any(normalize_rnti(v) == rnti for v in event["ue_ids"])
    # MAC and RLC events without identifiers in ue_ids still name them in the summary.
    return any(f"{tag}={ue} " in event["summary"] + " " for tag in ("rnti", "ueid"))
