# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""NGAP pcaps: the UEs with their identifiers, and the procedures of each UE."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from typing import Any

from ._ues import ue_ids_by_key
from .messages import cause, valid_cause_fields
from .tshark import Tshark
from .values import epoch_to_iso, first

PROCEDURE_FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "ngap.procedureCode",
    "ngap.RAN_UE_NGAP_ID",
    "ngap.AMF_UE_NGAP_ID",
    "ngap.unsuccessfulOutcome_element",
]


def ue_ids(tshark: Tshark, pcap: str | os.PathLike[str], *, force: bool = False) -> list[dict[str, Any]]:
    """The UEs of an NGAP pcap by RAN-UE-NGAP-ID, with the AMF-UE-NGAP-ID once assigned."""
    return ue_ids_by_key(
        tshark, pcap, "ngap", "ngap.RAN_UE_NGAP_ID", "ngap.AMF_UE_NGAP_ID",
        ("ran_ue_ngap_id", "amf_ue_ngap_id"), "ngap-ue-ids-v1", force,
    )


def procedures(
    tshark: Tshark, pcap: str | os.PathLike[str], *, ue: str | None = None, failures_only: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """The NGAP messages of each RAN-UE-NGAP-ID in time order, optionally of one UE or only unsuccessful outcomes.

    Messages without a RAN-UE-NGAP-ID are under "(no-ran-ue-id)". A cause does not make a message a failure, since
    successful messages carry one too, e.g. PDUSessionResourceRelease.
    """
    display_filter = f"ngap.RAN_UE_NGAP_ID == {ue}" if ue else None
    if failures_only:
        unsuccess = "ngap.unsuccessfulOutcome_element"
        display_filter = f"({display_filter}) && ({unsuccess})" if display_filter else unsuccess
    fields = PROCEDURE_FIELDS + valid_cause_fields(tshark, pcap, "ngap")
    # The fields are part of the tag, since they depend on the tshark build and this code.
    digest = hashlib.sha256("\0".join(fields).encode()).hexdigest()[:16]
    tag = f"ngap-proc-{digest}-{ue or 'all'}-{int(failures_only)}"
    by_ue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for values in tshark.iter_fields(pcap, fields, display_filter=display_filter, tag=tag):
        frame, epoch, code, ran_id, amf_id, unsucc = values[:6]
        ran_id, amf_id = first(ran_id), first(amf_id)
        by_ue[ran_id or "(no-ran-ue-id)"].append({
            "frame": int(frame) if frame else None,
            "epoch": float(epoch) if epoch else None,
            "iso": epoch_to_iso(epoch) if epoch else None,
            "procedureCode": code,
            "amfUeId": amf_id or None,
            "failure": bool(unsucc),
            "cause": cause("ngap", dict(zip(fields, values))),
        })
    return dict(by_ue)
