# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""UE contexts of an NGAP, F1AP or E1AP pcap: the messages of each UE, from its first message to the release of its
context, with its UE identifiers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..correlate.ues import CREATION_SLACK_S, RNTI_SLACK_S
from ..ran.rnti import normalize as normalize_rnti
from .messages import UE_ID_FIELDS

# Procedure code that releases the UE context of each protocol, whose successful outcome ends the context.
RELEASE_CODES = {"ngap": "41", "f1ap": "6", "e1ap": "11"}
# Short names of the UE identifiers, by message field.
ID_NAMES = {
    "ran_ue_ngap_id": "ran_ngap",
    "amf_ue_ngap_id": "amf_ngap",
    "du_ue_f1ap_id": "du_f1ap",
    "cu_ue_f1ap_id": "cu_f1ap",
    "cu_cp_ue_e1ap_id": "cu_cp_e1ap",
    "cu_up_ue_e1ap_id": "cu_up_e1ap",
}
# Messages whose GTP TEIDs are the UL ones of the UPF, which NGAP and E1AP share, by protocol: NGAP
# HandoverResourceAllocation, InitialContextSetup and PDUSessionResourceSetup requests, E1AP BearerContextSetup
# requests. The TEIDs that the gNB allocates repeat across UEs.
_UPF_TEID_CODES = {"ngap": {"13", "14", "29"}, "e1ap": {"8"}}
# InitialULRRCMessageTransfer, whose RRC reestablishment request holds the old C-RNTI of the UE.
_INITIAL_UL_RRC_CODE = "11"


@dataclass
class UeContext:
    """UE context of a pcap."""

    id: int
    t_start: float
    t_end: float
    # UE identifiers by short name, e.g. {"du_f1ap": "0", "cu_f1ap": "0"}, the first value of each.
    ids: dict[str, str] = field(default_factory=dict)
    # C-RNTI, as logs print it, for F1AP.
    rnti: str | None = None
    released: bool = False
    # Values that link it to other contexts, with the time of their first message, see ContextTracker: "ho_rnti"
    # (target C-RNTIs of its handovers), "reest_rnti" (C-RNTI of the context it reestablishes), "nas" (hashes of its
    # NAS PDUs) and "teid" (UPF TEIDs).
    links: dict[str, dict[str, float]] = field(default_factory=dict)

    def label(self) -> str:
        """Its identifiers, e.g. "du_f1ap=0 cu_f1ap=0"."""
        return " ".join(f"{name}={self.ids[name]}" for name in ID_NAMES.values() if name in self.ids)


class ContextTracker:
    """Assigns the messages of a pcap of proto, "ngap", "f1ap" or "e1ap", to UE contexts, given in file order.

    A message belongs to the context of any of its UE identifiers in use. Identifiers are free again once the context
    is released, since later UEs reuse them. An F1AP context is also released by the reestablishment that names it by
    its old gNB-DU UE F1AP ID, as the DU then frees it without a UE context release.
    """

    def __init__(self, proto: str):
        self.proto = proto
        self.contexts: list[UeContext] = []
        self._fields = [label for label, _ in UE_ID_FIELDS[proto]]
        self._in_use: dict[tuple[str, str], UeContext] = {}

    def assign(self, msg: dict[str, Any]) -> UeContext | None:
        """Returns the context of a message, or None for messages of no UE. The messages of capture.read() give contexts
        their C-RNTI and links; those of messages.messages(), which carry neither, give contexts without them.
        """
        ids = {f: msg[f] for f in self._fields if msg.get(f)}
        ctx = next((self._in_use[(k, v)] for k, v in ids.items() if (k, v) in self._in_use), None)
        if ctx is None and ids:
            ctx = UeContext(len(self.contexts), msg["epoch"], msg["epoch"])
            self.contexts.append(ctx)
        if ctx is None:
            return None
        ctx.t_end = max(ctx.t_end, msg["epoch"])
        for k, v in ids.items():
            self._in_use[(k, v)] = ctx
            ctx.ids.setdefault(ID_NAMES[k], v)
        if msg.get("crnti") and ctx.rnti is None:
            ctx.rnti = normalize_rnti(msg["crnti"])
        for label, values in (msg.get("links") or {}).items():
            for value in self._link_values(label, values, msg):
                ctx.links.setdefault(label, {}).setdefault(value, msg["epoch"])
        if msg["code"] == RELEASE_CODES[self.proto] and msg["outcome"] == "successful":
            ctx.released = True
            for k, v in ids.items():
                self._in_use.pop((k, v), None)
        if (old_id := msg.get("old_du_ue_f1ap_id")) and (old := self._in_use.get(("du_ue_f1ap_id", old_id))) not in (None, ctx):
            self._release(old, msg["epoch"])
        return ctx

    def _release(self, ctx: UeContext, epoch: float) -> None:
        """Releases a context at a time, freeing its identifiers."""
        ctx.released = True
        ctx.t_end = max(ctx.t_end, epoch)
        for key in [key for key, c in self._in_use.items() if c is ctx]:
            del self._in_use[key]

    def _link_values(self, label: str, values: list[str], msg: dict[str, Any]) -> list[str]:
        """The values of a link of a message that link its context, normalized, e.g. NAS PDUs as hashes."""
        if label in ("ho_rnti", "reest_rnti"):
            if label == "reest_rnti" and msg["code"] != _INITIAL_UL_RRC_CODE:
                return []
            return [r for v in values if (r := normalize_rnti(v))]
        if label == "nas":
            return [hashlib.sha1(v.lower().encode()).hexdigest()[:16] for v in values]
        if label == "teid":
            if msg["outcome"] != "initiating" or msg["code"] not in _UPF_TEID_CODES.get(self.proto, ()):
                return []
            return [v.lower() for v in values]
        return values


def ue_contexts(proto: str, msgs: list[dict[str, Any]]) -> list[UeContext]:
    """The UE contexts of the messages of a pcap of proto, see ContextTracker."""
    tracker = ContextTracker(proto)
    for msg in msgs:
        tracker.assign(msg)
    return tracker.contexts


def ue_trace(contexts: list[UeContext], rnti: str) -> list[UeContext]:
    """The F1AP contexts of a C-RNTI, e.g. "0x4601" or "17921", and those that continue them, in start order: the
    targets of their handovers and the contexts that reestablish them, and so on. The contexts need the C-RNTIs and
    links of capture.read() messages.

    A handover target is the context of the target C-RNTI created nearest the message naming it, within
    CREATION_SLACK_S. A reestablishment continues the latest context of its old C-RNTI alive at its request, within
    RNTI_SLACK_S.
    """
    by_rnti: dict[str, list[UeContext]] = {}
    for ctx in contexts:
        if ctx.rnti:
            by_rnti.setdefault(ctx.rnti, []).append(ctx)
    reestablished_by: dict[int, list[UeContext]] = {}
    for ctx in contexts:
        for old_rnti, t in ctx.links.get("reest_rnti", {}).items():
            olds = [c for c in by_rnti.get(old_rnti, ()) if c is not ctx and c.t_start <= t <= c.t_end + RNTI_SLACK_S]
            if old_rnti != ctx.rnti and olds:
                reestablished_by.setdefault(max(olds, key=lambda c: c.t_start).id, []).append(ctx)
    found = {ctx.id: ctx for ctx in by_rnti.get(normalize_rnti(rnti), ())}
    todo = list(found.values())
    while todo:
        ctx = todo.pop()
        nexts = list(reestablished_by.get(ctx.id, ()))
        # The UEContextSetupResponse of a handover target names its own C-RNTI as newUE-Identity too.
        for new_rnti, t in ctx.links.get("ho_rnti", {}).items():
            near = [c for c in by_rnti.get(new_rnti, ()) if c is not ctx and abs(c.t_start - t) <= CREATION_SLACK_S]
            if new_rnti != ctx.rnti and near:
                nexts.append(min(near, key=lambda c: abs(c.t_start - t)))
        for nxt in nexts:
            if nxt.id not in found:
                found[nxt.id] = nxt
                todo.append(nxt)
    return sorted(found.values(), key=lambda c: (c.t_start, c.id))
