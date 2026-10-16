# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""F1AP pcaps: the UEs with their identifiers, and the messages with the RRC and NAS messages they carry."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from ..ran.nas import nas_name
from ..ran.procedures import proc_name
from ..ran.rrc import RRC_MESSAGE_TYPES, decode_ccch_type
from .tshark import Tshark
from .values import epoch_to_iso, first, to_int

UE_ID_FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "f1ap.procedureCode",
    "f1ap.GNB_DU_UE_F1AP_ID",
    "f1ap.GNB_CU_UE_F1AP_ID",
    "f1ap.C_RNTI",
]

MESSAGE_FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "f1ap.procedureCode",
    "f1ap.GNB_DU_UE_F1AP_ID",
    "f1ap.GNB_CU_UE_F1AP_ID",
    "f1ap.C_RNTI",
    "f1ap.SRBID",
    "f1ap.RRCContainer",
]
NAS_FIELDS = [
    # The names before tshark 4.2, which later builds keep as aliases of nas-5gs.*.
    "nas_5gs.mm.message_type",
    "nas_5gs.sm.message_type",
]

# Procedures carrying an RRC container, and the downlink ones among them.
_RRC_PROCS = {5, 6, 7, 11, 12, 13}
_DL_PROCS = {5, 6, 7, 12}


def ue_ids(tshark: Tshark, pcap: str | os.PathLike[str], *, force: bool = False) -> list[dict[str, Any]]:
    """The UEs of an F1AP pcap in order of appearance, each with its first frame and message and its identifiers.

    A UE is identified by its gNB-CU-UE-F1AP-ID, or its gNB-DU-UE-F1AP-ID before the CU assigns one.
    """
    rows = []
    for frame, epoch, code, du_id, cu_id, crnti in tshark.iter_fields(pcap, UE_ID_FIELDS, tag="f1ap-ue-ids-v1", force=force):
        if epoch:
            rows.append({
                "frame": int(frame) if frame else None,
                "epoch": float(epoch),
                "code": code,
                "du_ue_id": first(du_id),
                "cu_ue_id": first(cu_id),
                "crnti": crnti or None,
            })
    clusters = sorted(_build_clusters(rows), key=lambda c: c["first_epoch"])
    return [
        {
            "frame": c["frame"],
            "first_iso": epoch_to_iso(c["first_epoch"]),
            "message": proc_name("f1ap", c["first_code"], with_code=False),
            "cu_ue_f1ap_id": c["cu_ue_f1ap_id"],
            "du_ue_f1ap_ids": sorted(c["du_ue_f1ap_ids"]),
            "crntis": sorted(c["crntis"]),
            "first_epoch": c["first_epoch"],
        }
        for c in clusters
    ]


def messages(tshark: Tshark, pcap: str | os.PathLike[str], *, force: bool = False) -> list[dict[str, Any]]:
    """The messages of an F1AP pcap in time order, with the RRC message and the NAS message they carry, if any, and their
    time as "epoch" and as "first_iso", in milliseconds.
    """
    rrc_types = valid_rrc_types(tshark, pcap)
    fields = rrc_fields(rrc_types, valid_nas_fields(tshark, pcap))
    rows = []
    # The fields are part of the tag, since they depend on the tshark build and this code.
    tag = "f1ap-messages-" + hashlib.sha256("\0".join(fields).encode()).hexdigest()[:16]
    for values in tshark.iter_fields(pcap, fields, tag=tag, force=force):
        v = dict(zip(fields, values))
        if not v["frame.time_epoch"]:
            continue
        rows.append({
            "frame": int(v["frame.number"]) if v["frame.number"] else None,
            "epoch": float(v["frame.time_epoch"]),
            "code": v["f1ap.procedureCode"],
            "du_ue_id": first(v["f1ap.GNB_DU_UE_F1AP_ID"]),
            "cu_ue_id": first(v["f1ap.GNB_CU_UE_F1AP_ID"]),
            **carried(v, rrc_types),
        })
    rows.sort(key=lambda x: x["epoch"])
    return [
        {
            "frame": x["frame"],
            "epoch": x["epoch"],
            "first_iso": epoch_to_iso(x["epoch"]),
            "message": proc_name("f1ap", x["code"], with_code=False),
            "rrc": x["rrc"],
            "nas": x["nas"],
            "cu_ue_f1ap_id": x["cu_ue_id"],
            "du_ue_f1ap_id": x["du_ue_id"],
            "crnti": x["crnti"],
        }
        for x in rows
    ]


def rrc_fields(rrc_types: list[str], nas_fields: list[str] | None = None) -> list[str]:
    """The tshark fields of the F1AP messages, of the RRC message types of rrc_types and of nas_fields, by default all
    of NAS_FIELDS, read by carried().
    """
    return MESSAGE_FIELDS + (NAS_FIELDS if nas_fields is None else nas_fields) + [_rrc_field(m) for m in rrc_types]


def carried(row: dict[str, str], rrc_types: list[str]) -> dict[str, str | None]:
    """The "rrc" message type, "nas" message type and "crnti" of an F1AP message, given its tshark field values by name,
    see rrc_fields().
    """
    detail = {
        "code": row["f1ap.procedureCode"],
        "srbid": row["f1ap.SRBID"] or None,
        "rrc_container": row["f1ap.RRCContainer"] or None,
        "rrc_elements": {m: row[_rrc_field(m)] for m in rrc_types},
    }
    return {
        "rrc": resolve_rrc(detail),
        "nas": nas_name(row.get("nas_5gs.mm.message_type"), row.get("nas_5gs.sm.message_type")),
        "crnti": row["f1ap.C_RNTI"] or None,
    }


def valid_rrc_types(tshark: Tshark, pcap: str | os.PathLike[str]) -> list[str]:
    """The RRC message types whose field this tshark build knows."""
    valid = _valid_optional_fields(tshark, pcap)
    return [m for m in RRC_MESSAGE_TYPES if _rrc_field(m) in valid]


def valid_nas_fields(tshark: Tshark, pcap: str | os.PathLike[str]) -> list[str]:
    """The NAS_FIELDS that this tshark build knows."""
    valid = _valid_optional_fields(tshark, pcap)
    return [f for f in NAS_FIELDS if f in valid]


def _valid_optional_fields(tshark: Tshark, pcap: str | os.PathLike[str]) -> set[str]:
    """The RRC and NAS fields that this tshark build knows."""
    # One list, so that tshark checks them in one run.
    return set(tshark.valid_fields(pcap, [*NAS_FIELDS, *(_rrc_field(m) for m in RRC_MESSAGE_TYPES)]))


def resolve_rrc(row: dict[str, Any]) -> str | None:
    """RRC message type of the RRC container of a message row, or None without one: tshark's dissection, else decoded
    or inferred from the container.
    """
    # RRC messages outside an RRC container are not sent to the UE, e.g. the source's rrcReconfiguration in the
    # HandoverPreparationInformation of the UEContextSetupRequest of a handover target.
    if not row["rrc_container"]:
        return None
    for name, val in row["rrc_elements"].items():
        if val:
            return name
    # tshark does not dissect the container of a UEContextReleaseCommand.
    code = to_int(row["code"])
    if code not in _RRC_PROCS:
        return None
    direction = "dl" if code in _DL_PROCS else "ul"
    # SRB0 is CCCH. InitialULRRCMessageTransfer (11) carries no SRBID but is always on SRB0.
    if code == 11 or to_int(row["srbid"]) == 0:
        return decode_ccch_type(row["rrc_container"], direction)
    # A DCCH container of a UEContextReleaseCommand is PDCP-protected, and in OCUDU it is an rrcRelease.
    if code == 6:
        return "rrcRelease?"
    return "(undissected)"


def _rrc_field(msg: str) -> str:
    return f"nr-rrc.{msg}_element"


def _build_clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    by_cu: dict[str, dict[str, Any]] = {}
    by_du: dict[str, dict[str, Any]] = {}
    for row in rows:
        # Rows without UE identifiers are procedures of the interface, e.g. F1Setup.
        if not (row["cu_ue_id"] or row["du_ue_id"] or row["crnti"]):
            continue
        c = None
        if row["cu_ue_id"] and row["cu_ue_id"] in by_cu:
            c = by_cu[row["cu_ue_id"]]
        if c is None and row["du_ue_id"] and row["du_ue_id"] in by_du:
            c = by_du[row["du_ue_id"]]
        if c is None:
            c = {
                "frame": row["frame"],
                "first_epoch": row["epoch"],
                "first_code": row["code"],
                "cu_ue_f1ap_id": None,
                "du_ue_f1ap_ids": set(),
                "crntis": set(),
            }
            clusters.append(c)
        if row["epoch"] < c["first_epoch"]:
            c["first_epoch"] = row["epoch"]
            c["frame"] = row["frame"]
            c["first_code"] = row["code"]
        if row["cu_ue_id"]:
            if c["cu_ue_f1ap_id"] is None:
                c["cu_ue_f1ap_id"] = row["cu_ue_id"]
            by_cu[row["cu_ue_id"]] = c
        if row["du_ue_id"]:
            c["du_ue_f1ap_ids"].add(row["du_ue_id"])
            by_du[row["du_ue_id"]] = c
        if row["crnti"]:
            c["crntis"].add(row["crnti"])
    return clusters
