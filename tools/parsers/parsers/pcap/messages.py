# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Messages of NGAP, F1AP and E1AP pcaps, with their procedure, outcome, UE identifiers and cause."""

from __future__ import annotations

import functools
import hashlib
import os
import shutil
import subprocess
from typing import Any

from ..ran.procedures import proc_name
from .tshark import Tshark
from .values import first, to_int

# UE identifier fields of each protocol, by label.
UE_ID_FIELDS: dict[str, list[tuple[str, str]]] = {
    "ngap": [("ran_ue_ngap_id", "ngap.RAN_UE_NGAP_ID"), ("amf_ue_ngap_id", "ngap.AMF_UE_NGAP_ID")],
    "f1ap": [("du_ue_f1ap_id", "f1ap.GNB_DU_UE_F1AP_ID"), ("cu_ue_f1ap_id", "f1ap.GNB_CU_UE_F1AP_ID")],
    "e1ap": [("cu_cp_ue_e1ap_id", "e1ap.GNB_CU_CP_UE_E1AP_ID"), ("cu_up_ue_e1ap_id", "e1ap.GNB_CU_UP_UE_E1AP_ID")],
}
# Fields whose values link the UE contexts of a run across messages and pcaps, by label: the C-RNTI of the target of a
# handover (newUE-Identity), in F1AP and in the NGAP handover of a target gNB, and the old C-RNTI of a reestablishment,
# the NAS PDUs, which F1AP and NGAP both carry, and the GTP TEIDs, of which NGAP and E1AP share the UPF one. Values of
# several occurrences are comma-separated.
LINK_FIELDS: dict[str, dict[str, list[str]]] = {
    "f1ap": {
        "ho_rnti": ["nr-rrc.newUE_Identity"],
        "reest_rnti": ["nr-rrc.c_RNTI"],
        "nas": ["nr-rrc.dedicatedNAS_Message", "nr-rrc.DedicatedNAS_Message"],
    },
    "ngap": {"nas": ["ngap.NAS_PDU"], "teid": ["ngap.gTP_TEID"], "ho_rnti": ["nr-rrc.newUE_Identity"]},
    "e1ap": {"teid": ["e1ap.gTP_TEID"]},
}
# Groups of the Cause of each protocol, a CHOICE whose value tshark has in the field of its group, e.g. ngap.protocol,
# in the order of their CHOICE index.
CAUSE_GROUPS = {
    "ngap": ("radioNetwork", "transport", "nas", "protocol", "misc"),
    "f1ap": ("radioNetwork", "transport", "protocol", "misc"),
    "e1ap": ("radioNetwork", "transport", "protocol", "misc"),
}


def messages(tshark: Tshark, pcap: str | os.PathLike[str], proto: str, *, force: bool = False) -> list[dict[str, Any]]:
    """The messages of a pcap of proto, "ngap", "f1ap" or "e1ap", in file order.

    Each has "frame", "epoch", "code", "procedure" (its name), "outcome" ("initiating", "successful" or
    "unsuccessful"), "info" (the tshark summary, naming the message), the UE identifiers of UE_ID_FIELDS by label, None
    when absent, "cause", and for F1AP "old_du_ue_f1ap_id", the old gNB-DU UE F1AP ID of a reestablishment, or None.
    force runs tshark even on a cache hit.
    """
    names = fields(proto, valid_cause_fields(tshark, pcap, proto))
    # The fields are part of the tag, since they depend on the protocol and this code.
    tag = f"{proto}-messages-" + hashlib.sha256("\0".join(names).encode()).hexdigest()[:16]
    rows = (dict(zip(names, values)) for values in tshark.iter_fields(pcap, names, tag=tag, force=force))
    return [m for row in rows if (m := message(proto, row)) is not None]


def fields(proto: str, causes: list[str] | None = None) -> list[str]:
    """The tshark fields of the messages of proto, read by message(), with the cause fields causes, by default all of
    cause_fields().
    """
    return [
        "frame.number",
        "frame.time_epoch",
        f"{proto}.procedureCode",
        f"{proto}.successfulOutcome_element",
        f"{proto}.unsuccessfulOutcome_element",
        "_ws.col.Info",
        *(cause_fields(proto) if causes is None else causes),
        *(f for _, f in UE_ID_FIELDS[proto]),
    ]


def cause_fields(proto: str) -> list[str]:
    """The tshark fields of the causes of proto, read by cause()."""
    # tshark names the CHOICE index of a Cause IE <proto>.Cause, and that of the cause of a list item <proto>.cause.
    return [f"{proto}.Cause", f"{proto}.cause", *_group_fields(proto)]


def valid_cause_fields(tshark: Tshark, pcap: str | os.PathLike[str], proto: str) -> list[str]:
    """The cause_fields() of proto that this tshark build knows."""
    return tshark.valid_fields(pcap, cause_fields(proto))


def _group_fields(proto: str) -> list[str]:
    """The tshark fields of the values of the Cause groups of proto."""
    return [f"{proto}.{group}" for group in CAUSE_GROUPS[proto]]


def cause(proto: str, row: dict[str, str]) -> str | None:
    """The cause of a message of proto, given its tshark field values by name, as "<group>:<value>", e.g.
    "protocol:unspecified", or None without one.

    It is the message's Cause IE. A message without one but with causes of list items, e.g. the PDU sessions that failed
    to set up of a PDUSessionResourceSetupResponse, has the cause of its first item.
    """
    groups = CAUSE_GROUPS[proto]
    ie_index = to_int(first(row.get(f"{proto}.Cause")))
    index = ie_index if ie_index is not None else to_int(first(row.get(f"{proto}.cause")))
    if index is None or index >= len(groups):
        return None
    group = groups[index]
    # Items may have causes of the group of the Cause IE, whose values tshark lists with its value in frame order.
    # Messages with both list the Cause IE after the item lists, e.g. InitialContextSetupFailure.
    value = (_last if ie_index is not None else first)(row.get(f"{proto}.{group}"))
    if not value:
        return None
    return f"{group}:{_cause_names().get(f'{proto}.{group}', {}).get(value, value)}"


@functools.lru_cache(maxsize=1)
def _cause_names() -> dict[str, dict[str, str]]:
    """Names of the values of the cause fields, by field and value, from the value tables of the tshark on PATH, so that
    they match its build; empty without tshark.
    """
    tshark = shutil.which("tshark")
    if tshark is None:
        return {}
    wanted = {f for proto in CAUSE_GROUPS for f in _group_fields(proto)}
    names: dict[str, dict[str, str]] = {}
    with subprocess.Popen([tshark, "-G", "values"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as proc:
        for line in proc.stdout:
            # Value lines are "V", the field, the value and its name, tab-separated.
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 4 and parts[0] == "V" and parts[1] in wanted:
                names.setdefault(parts[1], {})[parts[2]] = parts[3]
    return names


def link_fields(proto: str) -> list[str]:
    """The tshark fields of LINK_FIELDS of proto, read by links()."""
    return [f for fs in LINK_FIELDS.get(proto, {}).values() for f in fs]


def links(proto: str, row: dict[str, str]) -> dict[str, list[str]]:
    """The values of the LINK_FIELDS of proto in a message, by label, given its tshark field values by name. Fields
    missing from the row, e.g. unknown to this tshark build, have none.
    """
    out: dict[str, list[str]] = {}
    for label, fs in LINK_FIELDS.get(proto, {}).items():
        values = [v for f in fs for v in (row.get(f) or "").split(",") if v]
        if values:
            out[label] = values
    return out


def message(proto: str, row: dict[str, str]) -> dict[str, Any] | None:
    """The message of proto in a frame, given its tshark field values by name, see messages(), or None without a frame
    number or time.
    """
    frame, epoch, code = row["frame.number"], row["frame.time_epoch"], row[f"{proto}.procedureCode"]
    if not frame or not epoch:
        return None
    if row[f"{proto}.unsuccessfulOutcome_element"]:
        outcome = "unsuccessful"
    elif row[f"{proto}.successfulOutcome_element"]:
        outcome = "successful"
    else:
        outcome = "initiating"
    return {
        "frame": int(frame),
        "epoch": float(epoch),
        "code": code,
        "procedure": proc_name(proto, code, with_code=False),
        "outcome": outcome,
        "info": row["_ws.col.Info"],
        # A message may also name an old identifier after the UE's own, e.g. the old gNB-DU UE F1AP ID of a
        # reestablishment.
        **{label: first(row[f]) for label, f in UE_ID_FIELDS[proto]},
        "cause": cause(proto, row),
        # Only the DLRRCMessageTransfer of a reestablishment names an old gNB-DU UE F1AP ID; a Reset lists the ids of
        # several UEs.
        **({"old_du_ue_f1ap_id": _second(row["f1ap.GNB_DU_UE_F1AP_ID"]) if code == "12" else None}
           if proto == "f1ap" else {}),
    }


def _last(value: str | None) -> str | None:
    """Last value of a tshark field of several occurrences, or None if empty."""
    return value.rsplit(",", 1)[-1] or None if value else None


def _second(value: str) -> str | None:
    """Second value of a tshark field of several occurrences, or None."""
    values = value.split(",")
    return values[1] or None if len(values) > 1 else None
