# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Summary of an OCUDU log in one pass: build, time span, NG setup, cells, UE lifecycle, procedure counters, warnings,
errors and shutdown.

Procedures are counted from the info lines of their loggers, so their counts are zero when those log at warning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import preamble

_UE_RE = re.compile(r"\bue=(\d+)\b")
_PCI_RE = re.compile(r"\bpci=(\d+)\b")
_CRNTI_RE = re.compile(r"c-rnti=0x([0-9a-fA-F]+)")
_BUILD_RE = re.compile(r"commit (\S+) on branch (\S+)")
# Older builds print the pci of a slot event in the event, not in the header.
_SLOT_EVENTS_RE = re.compile(r"Processed slot events(?: pci=(\d+))?:$")
# PDUs only: NGAP also names HandoverRequest in its line of the target cell of one.
_NGAP_HO_RE = re.compile(r"PDU\b.*: HandoverRe(?:quired|quest)$")
_BANNER_RE = re.compile(r"==== (.+) started ===")
# CU-CP line of an intra-CU handover completed, at info on the target side. The source side finishes at debug.
_INTRA_CU_HO_DONE = '"Intra CU Handover Target Routine" finished successfully'
# Warnings expected in healthy runs: per-UE creation latency instrumentation and host notes.
BENIGN_WARNINGS = (
    "DRM KMS polling is enabled",
    "hyperthreading",
    "UE creation (ue=",
    "MAC UE creation (ue=",
)
# Warning and error lines kept at most.
MAX_KEPT_LINES = 50
# RRC lines kept at most, and the messages they are kept for.
MAX_RRC_LINES = 200
_KEPT_RRC = ("rrcSetup", "rrcReconfiguration", "securityModeCommand", "securityModeComplete", "rrcRelease",
             "rrcReestablishment", "rrcReject")


@dataclass
class LogSummary:
    """Summary of a log. Times are its timestamps as text, and UE indexes and RNTIs as logged, RNTIs in hex."""

    build_commit: str | None = None
    build_branch: str | None = None
    # Application of the "==== <app> started ===" banner: "gNB", "DU" or "CU".
    app: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    # Whether the workers stopped successfully.
    clean_shutdown: bool = False
    # "success", "failure" or None when no NG setup outcome is logged.
    ng_setup: str | None = None
    amf_connected_ts: str | None = None
    # (time, PCI) of each cell created.
    cells_created: list[tuple[str, int]] = field(default_factory=list)
    # (time, UE index, C-RNTI) of each UE created by the CU-CP, "?" for an absent one.
    ue_creates: list[tuple[str, str, str]] = field(default_factory=list)
    # (time, UE index) of each UE released by the CU-CP.
    ue_releases: list[tuple[str, str]] = field(default_factory=list)
    # (time, UE index) of each successful Initial Context Setup.
    init_ctx_done: list[tuple[str, str]] = field(default_factory=list)
    rrc_reconfigs: int = 0
    # Handovers completed, counted on the target side: intra-CU ones and NGAP ones, by their HandoverNotify. Xn and
    # conditional inter-CU ones have no info line of completion.
    ho_count: int = 0
    # reconfigurationWithSync in RRC bodies, which RRC logs at debug.
    reconf_with_sync_count: int = 0
    # NGAP HandoverRequired and HandoverRequest PDUs.
    ngap_handover_count: int = 0
    reest_count: int = 0
    # E1AP bearer context procedures, counted on the CU-CP side only.
    bearer_setups: int = 0
    bearer_modifications: int = 0
    bearer_releases: int = 0
    prach_events: int = 0
    # PHY CRC failures (crc=KO).
    crc_fails: int = 0
    # (time, logger, message) of the first non-benign warnings and of the first errors, MAX_KEPT_LINES at most, and the
    # numbers of all of them.
    warnings: list[tuple[str, str, str]] = field(default_factory=list)
    warning_count: int = 0
    benign_warning_count: int = 0
    errors: list[tuple[str, str, str]] = field(default_factory=list)
    error_count: int = 0
    # UE indexes, RNTIs and PCIs seen, sorted.
    ues_seen: list[str] = field(default_factory=list)
    rntis_seen: list[str] = field(default_factory=list)
    pcis_seen: list[int] = field(default_factory=list)
    # (time, logger, message) of the first key RRC lines, see _KEPT_RRC.
    rrc_msgs: list[tuple[str, str, str]] = field(default_factory=list)
    metrics_rows: int = 0
    # Message of the last METRICS line.
    last_metrics: str | None = None
    # Time the application started shutting down, by closing its pcaps.
    shutdown_signal_ts: str | None = None


def is_benign_warning(msg: str) -> bool:
    """Whether a warning message is expected in healthy runs, see BENIGN_WARNINGS."""
    return any(s in msg for s in BENIGN_WARNINGS)


def summarize(lines: Iterable[str]) -> LogSummary:
    """The summary of the lines of a log."""
    out = LogSummary()
    ues, rntis, pcis = set(), set(), set()
    # Time and PCI, if any, of the entry of scheduler slot events at debug, whose continuation lines hold the events.
    slot_events_ts: str | None = None
    slot_events_pci: int | None = None
    for raw in lines:
        # The configuration echo and continuation lines do not start with a timestamp, so skip them before matching.
        if not raw or raw[0] not in "0123456789":
            # The handover marker is the ASN.1 or JSON brace of the reconfigurationWithSync of an RRC body.
            if "reconfigurationWithSync" in raw and "{" in raw:
                out.reconf_with_sync_count += 1
            # The scheduler prints its slot events one per continuation line at debug.
            if slot_events_ts is not None:
                if raw.startswith("- PRACH: "):
                    out.prach_events += 1
                elif raw.startswith("- Cell creation: "):
                    pci = int(pm.group(1)) if (pm := _PCI_RE.search(raw)) else slot_events_pci
                    if pci is not None:
                        out.cells_created.append((slot_events_ts, pci))
                        pcis.add(pci)
            continue
        m = preamble.match_header(raw)
        if not m:
            continue
        ts = m.group("timestamp")
        layer = m.group("layer").strip()
        lvl = m.group("level") or ""
        msg = raw[m.end() :].rstrip("\r\n")
        slot_events_ts = None
        if layer == "SCHED" and msg.endswith(":") and (sm := _SLOT_EVENTS_RE.search(msg)):
            slot_events_ts, slot_events_pci = ts, int(sm.group(1)) if sm.group(1) else None
        if out.first_ts is None:
            out.first_ts = ts
        out.last_ts = ts

        if lvl == "E":
            out.error_count += 1
            if len(out.errors) < MAX_KEPT_LINES:
                out.errors.append((ts, layer, msg.strip()))
        elif lvl == "W":
            if is_benign_warning(msg):
                out.benign_warning_count += 1
            else:
                out.warning_count += 1
                if len(out.warnings) < MAX_KEPT_LINES:
                    out.warnings.append((ts, layer, msg.strip()))

        # The CU application logs its build and banner as CU.
        if layer in ("GNB", "CU"):
            if "Built in" in msg and (bm := _BUILD_RE.search(msg)):
                out.build_commit, out.build_branch = bm.group(1), bm.group(2)
            elif out.app is None and (am := _BANNER_RE.search(msg)):
                out.app = am.group(1)

        if layer == "NGAP":
            if "NGSetupResponse" in msg:
                out.ng_setup = "success"
            elif "NGSetupFailure" in msg:
                out.ng_setup = "failure"
            if _NGAP_HO_RE.search(msg):
                out.ngap_handover_count += 1
            elif msg.startswith("Tx PDU") and msg.endswith(": HandoverNotify"):
                out.ho_count += 1

        if layer == "CU-CP" and "Connection to AMF" in msg and "established" in msg:
            out.amf_connected_ts = ts

        if layer == "SCHED" and "Cell creation idx=" in msg and (pm := _PCI_RE.search(msg)):
            pci = int(pm.group(1))
            out.cells_created.append((ts, pci))
            pcis.add(pci)

        if layer == "SCHED" and "prach(" in msg:
            out.prach_events += msg.count("prach(")

        if layer == "PHY" and "crc=KO" in msg:
            out.crc_fails += 1

        if layer == "CU-CP" and ": UE created" in msg:
            ue = um.group(1) if (um := _UE_RE.search(msg)) else "?"
            rnti = rm.group(1) if (rm := _CRNTI_RE.search(msg)) else "?"
            out.ue_creates.append((ts, ue, rnti))
            ues.add(ue)
            if rnti != "?":
                rntis.add(rnti)

        if layer == "CU-CP" and '"Initial Context Setup Routine" finished successfully' in msg:
            out.init_ctx_done.append((ts, um.group(1) if (um := _UE_RE.search(msg)) else "?"))

        if layer == "CU-CP" and _INTRA_CU_HO_DONE in msg:
            out.ho_count += 1

        if layer == "CU-CP" and '"UE Removal Routine" finished successfully' in msg:
            out.ue_releases.append((ts, um.group(1) if (um := _UE_RE.search(msg)) else "?"))

        # At debug, each message is followed by its JSON in an entry of its own, "Containerized <message>: [".
        if layer == "RRC" and "Containerized " not in msg:
            if "rrcReconfiguration" in msg and "Complete" not in msg:
                out.rrc_reconfigs += 1
            if "rrcReestablishmentRequest" in msg or "reestablishmentRequest" in msg:
                out.reest_count += 1
            if any(k in msg for k in _KEPT_RRC) and len(out.rrc_msgs) < MAX_RRC_LINES:
                out.rrc_msgs.append((ts, layer, msg.strip()))

        # Bearer procedures are logged by both the CU-CP and the CU-UP: count them once.
        if layer == "CU-CP-E1":
            if "BearerContextSetupResponse" in msg:
                out.bearer_setups += 1
            elif "BearerContextModificationResponse" in msg:
                out.bearer_modifications += 1
            elif "BearerContextReleaseComplete" in msg:
                out.bearer_releases += 1

        if layer == "METRICS":
            out.metrics_rows += 1
            out.last_metrics = msg

        if "Closing PCAP files" in msg and out.shutdown_signal_ts is None:
            out.shutdown_signal_ts = ts
        if "Workers stopped successfully" in msg:
            out.clean_shutdown = True

    out.ues_seen = sorted(ues, key=lambda s: int(s) if s.isdigit() else -1)
    out.rntis_seen = sorted(rntis)
    out.pcis_seen = sorted(pcis)
    return out
