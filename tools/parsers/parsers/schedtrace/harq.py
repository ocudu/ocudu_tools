# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""HARQ transactions of a schedtrace: the life of a transport block on a HARQ process of a UE, from its new
transmission through its retransmissions, with the PDCCHs that schedule them, the UCI that carries their HARQ-ACK and
the HARQ-ACK reports, so that one allocation can be followed to its outcome.

A transaction of a process, the (RNTI, HARQ id) of a direction, starts at a PDSCH or PUSCH without retransmissions
before it and lasts until a HARQ-ACK of Ack, or the next new transmission on the process. The trace has no link
between these, so they are matched as OCUDU schedules them:

- A DL PDCCH and its PDSCH are in the same slot decision, of the same RNTI and HARQ id.
- A UL PDCCH is in the slot decision k2 slots before its PUSCH, which the slot decision of its transmission holds.
- The UCI of a PDSCH is at k1 slots after it: a PUCCH of the RNTI with HARQ-ACK bits, which may carry those of other
  PDSCHs, else a PUSCH of the RNTI in that slot, which the trace does not say carries UCI.
- A HARQ-ACK report is of the RNTI and HARQ id, received at that UCI slot (slot_rx).

The trace has no UL CRC, so UL transactions have no outcome, and no event of HARQ processes that time out or reach
their maximum retransmissions: a DL transaction without Ack is ended by the next new transmission on its process.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Hashable

from ..ran.rnti import normalize
from .grid import MAX_AGGR_LVL_INDEX
from .ue_metrics import ue_index

# Frames after which the SFN wraps.
NOF_SFNS = 1024
# Names of DlPdcch.dci_type values, dci_dl_rnti_config_type of OCUDU, and of UlPdcch.dci_type values,
# dci_ul_rnti_config_type.
DL_DCI_TYPES = ("si-rnti 1_0", "ra-rnti 1_0", "c-rnti 1_0", "tc-rnti 1_0", "p-rnti 1_0", "c-rnti 1_1")
UL_DCI_TYPES = ("tc-rnti 0_0", "c-rnti 0_0", "c-rnti 0_1", "cs-rnti 0_1")
# HarqAckReportStatus values, as the decoder names them.
_STATUSES = ("Nack", "Ack", "Dtx")
# Largest k2 of a PUSCH, TS 38.331 PUSCH-TimeDomainResourceAllocation, beyond which a UL PDCCH is not matched to one.
_MAX_K2 = 32
# Kinds of the rows of a transaction.
PDCCH, PDSCH, PUSCH, PUCCH, UCI_ON_PUSCH, HARQ_ACK = "PDCCH", "PDSCH", "PUSCH", "PUCCH", "UCI on PUSCH", "HARQ-ACK"


@dataclass
class Row:
    """A PDU or report of a transaction: its kind, the key the caller gave its slot event, the slot_tx of that event,
    its time in seconds, the transmission it belongs to (1 for the new transmission), the [list, index] of its
    allocation in the slot decision, None for a report, and its fields.
    """

    kind: str
    event: Hashable
    slot_tx: int
    t: float
    tx: int
    item: tuple[str, int] | None
    params: dict[str, Any]


@dataclass
class Transaction:
    """A transport block on a HARQ process: direction ("DL" or "UL"), RNTI as OCUDU logs print it, UE index, HARQ id,
    TBS in bytes of its new transmission, its rows in slot order, and how it ended: "ack", "new_tx" (the next new
    transmission on its process) or "end" (the end of the events). started is False when its new transmission is
    before the events.
    """

    dir: str
    rnti: str
    ue_index: int | None
    harq_id: int
    tbs: int | None = None
    rows: list[Row] = field(default_factory=list)
    ended_by: str = "end"
    started: bool = True
    # Transmissions so far, and the HARQ-ACK statuses received.
    nof_tx: int = 0
    statuses: list[str] = field(default_factory=list)
    # UCI slots, unwrapped, of the PDSCHs of a DL transaction, with their transmission.
    _uci_slots: dict[int, int] = field(default_factory=dict, repr=False)
    # RNTI as in the trace.
    _rnti: int = field(default=0, repr=False)

    @property
    def acked(self) -> bool:
        return self.ended_by == "ack"

    def outcome(self) -> str:
        """One line of how it ended, e.g. "ACK at tx 2" or "failed after 4 NACK/DTX"."""
        if self.dir == "UL":
            return "outcome unknown, the trace has no UL CRC"
        if self.acked:
            return f"ACK at tx {self.nof_tx}"
        nok = sum(s != "Ack" for s in self.statuses)
        if not nok:
            return "no HARQ-ACK report" + (" by the end of the events" if self.ended_by == "end" else "")
        if self.ended_by == "end":
            return f"no ACK after {nok} NACK/DTX by the end of the events"
        return f"failed after {nok} NACK/DTX"

    def has(self, event: Hashable, item: tuple[str, int]) -> bool:
        """Whether an allocation, by the key of its slot event and its [list, index], is one of its PDCCHs, PDSCHs or
        PUSCHs.
        """
        return any(r.event == event and r.item == item and r.kind in (PDCCH, PDSCH, PUSCH) for r in self.rows)


def transactions(events: Iterable[tuple[Hashable, dict[str, Any]]], slots_per_frame: int) -> Iterator[Transaction]:
    """The HARQ transactions of CellSlotEvents of schedtrace.read() in slot order, each given with a key, e.g. its
    record, that its rows name. They are yielded as they end, those still open at the end of the events last. Other
    events are skipped.
    """
    tracker = _Tracker(slots_per_frame)
    for key, event in events:
        if event.get("type") == "CellSlotEvent":
            yield from tracker.add(key, event)
    yield from tracker.finish()


def find(events: Iterable[tuple[Hashable, dict[str, Any]]], slots_per_frame: int, event: Hashable,
         item: tuple[str, int]) -> Transaction | None:
    """The transaction of an allocation, a PDSCH, PUSCH or PDCCH given by the key of its slot event and its
    [list, index] in the slot decision, among those of transactions(), or None. Reading the events stops once it ends.
    """
    item = tuple(item)
    for tr in transactions(events, slots_per_frame):
        if tr.has(event, item):
            return tr
    return None


class _Tracker:
    """The open transactions of the slots added so far."""

    def __init__(self, slots_per_frame: int):
        self._wrap = NOF_SFNS * slots_per_frame
        self._spf = slots_per_frame
        # Unwrapped slot of the last slot added, and the offset that unwraps slot_tx.
        self._slot: int | None = None
        self._base = 0
        self._open: dict[tuple[str, int, int], Transaction] = {}
        # UL PDCCHs not yet matched to their PUSCH, by (RNTI, HARQ id), as (unwrapped slot, row).
        self._ul_pdcchs: dict[tuple[int, int], list[tuple[int, Row]]] = {}

    def add(self, key: Hashable, event: dict[str, Any]) -> list[Transaction]:
        slot = self._unwrap(event["slot_tx"])
        t = event["timestamp_us"] / 1e6
        decision = event.get("decision") or {}
        done: list[Transaction] = []

        def row(kind: str, tx: int, item: tuple[str, int] | None, params: dict[str, Any]) -> Row:
            return Row(kind, key, event["slot_tx"], t, tx, item, params)

        # Reports are received before the slot decision, which may start a new transmission on their process.
        for inp in event.get("inputs") or []:
            if not inp or inp.get("type") != "HarqAckEvent":
                continue
            tr = self._open.get(("DL", inp["rnti"], inp["harq_id"]))
            slot_rx = inp.get("slot_rx")
            rx = None if slot_rx is None else self._unwrap_near(slot_rx, slot)
            if tr is None or (rx is not None and rx not in tr._uci_slots):
                continue
            status = inp["status"]
            if not isinstance(status, str):
                status = _STATUSES[status] if 0 <= status < len(_STATUSES) else f"Status{status}"
            tx = tr._uci_slots.get(rx, tr.nof_tx)
            tr.rows.append(row(HARQ_ACK, tx, None,
                               {"status": status, "tbs": inp.get("tbs"), "slot_rx": self._sfn_slot(slot_rx)}))
            tr.statuses.append(status)
            if status == "Ack":
                tr.ended_by = "ack"
                done.append(self._open.pop(("DL", inp["rnti"], inp["harq_id"])))

        dl_pdcchs = decision.get("dl_pdcchs") or []
        for i, p in enumerate(decision.get("ue_pdschs") or []):
            cws = p.get("codewords") or [{}]
            tr = self._transaction("DL", p, done)
            pdcch = next(((j, c) for j, c in enumerate(dl_pdcchs)
                          if c["rnti"] == p["rnti"] and c["harq_id"] == p["harq_id"]), None)
            if pdcch is not None:
                tr.rows.append(row(PDCCH, tr.nof_tx, ("dl_pdcchs", pdcch[0]), _pdcch_params(pdcch[1], DL_DCI_TYPES)))
            params = {"mcs": cws[0].get("mcs_index"), "rv": cws[0].get("rv_index"),
                      "tbs": sum(c.get("tb_size_bytes") or 0 for c in cws), "new_data": cws[0].get("new_data"),
                      "k1": p["k1"], "nof_retxs": p["nof_retxs"], **_rbs(p), "sym": _syms(p)}
            if len(cws) > 1:
                params["mcs2"] = cws[1].get("mcs_index")
            tr.rows.append(row(PDSCH, tr.nof_tx, ("ue_pdschs", i), params))
            if tr.tbs is None:
                tr.tbs = params["tbs"]
            tr._uci_slots[slot + p["k1"]] = tr.nof_tx

        pending = self._ul_pdcchs
        for i, p in enumerate(decision.get("ul_pdcchs") or []):
            pending.setdefault((p["rnti"], p["harq_id"]), []).append(
                (slot, row(PDCCH, 0, ("ul_pdcchs", i), _pdcch_params(p, UL_DCI_TYPES))))
        puschs = decision.get("puschs") or []
        for i, p in enumerate(puschs):
            tr = self._transaction("UL", p, done)
            # The PDCCH of a PUSCH is k2 slots before it; a non-adaptive retransmission has none.
            queue = pending.get((p["rnti"], p["harq_id"])) or []
            for at, (pdcch_slot, pdcch_row) in enumerate(queue):
                if pdcch_slot == slot - p["k2"]:
                    pdcch_row.tx = tr.nof_tx
                    tr.rows.append(pdcch_row)
                    del queue[at]
                    break
            params = {"tbs": p["tb_size_bytes"], "rv": p["rv_index"], "k2": p["k2"], "nof_retxs": p["nof_retxs"],
                      "layers": p.get("nof_layers"), **_rbs(p), "sym": _syms(p)}
            tr.rows.append(row(PUSCH, tr.nof_tx, ("puschs", i), params))
            if tr.tbs is None:
                tr.tbs = p["tb_size_bytes"]
        # UL PDCCHs whose PUSCH is past the largest k2 are of PUSCHs never transmitted, or before the events.
        for k in list(pending):
            pending[k] = [x for x in pending[k] if slot - x[0] <= _MAX_K2]
            if not pending[k]:
                del pending[k]

        pucchs = decision.get("pucchs") or []
        for (direction, _, _), tr in self._open.items():
            if direction != "DL" or slot not in tr._uci_slots:
                continue
            tx = tr._uci_slots[slot]
            rnti = tr._rnti
            found = [(i, p) for i, p in enumerate(pucchs) if p["rnti"] == rnti and p.get("nof_bits_harq_ack")]
            for i, p in found:
                params = {"harq_bits": p["nof_bits_harq_ack"], "sr_bits": p.get("nof_bits_sr"),
                          "csi_bits": p.get("nof_bits_csi1"), "res": p.get("res_idx")}
                tr.rows.append(row(PUCCH, tx, ("pucchs", i), params))
            if not found:
                for i, p in enumerate(puschs):
                    if p["rnti"] == rnti:
                        params = {"harq_id": p["harq_id"], "inferred": True}
                        tr.rows.append(row(UCI_ON_PUSCH, tx, ("puschs", i), params))

        return done

    def finish(self) -> list[Transaction]:
        done = list(self._open.values())
        self._open.clear()
        return done

    def _transaction(self, direction: str, alloc: dict[str, Any], done: list[Transaction]) -> Transaction:
        """The transaction of a PDSCH or PUSCH, a new one for a new transmission, which ends the one of its process."""
        key = (direction, alloc["rnti"], alloc["harq_id"])
        tr = self._open.get(key)
        if alloc["nof_retxs"] == 0 or tr is None:
            if tr is not None:
                tr.ended_by = "new_tx"
                done.append(tr)
            tr = self._open[key] = Transaction(direction, normalize(alloc["rnti"]), ue_index(alloc.get("ue_index")),
                                               alloc["harq_id"], started=alloc["nof_retxs"] == 0, _rnti=alloc["rnti"])
        tr.nof_tx = alloc["nof_retxs"] + 1 if tr.started else tr.nof_tx + 1
        return tr

    def _unwrap(self, slot_tx: int) -> int:
        if self._slot is not None and slot_tx + self._base < self._slot - self._wrap // 2:
            self._base += self._wrap
        self._slot = slot_tx + self._base
        return self._slot

    def _unwrap_near(self, slot_tx: int, slot: int) -> int:
        """A slot_tx unwrapped to the nearest of an unwrapped slot."""
        s = slot_tx + self._base
        if s > slot + self._wrap // 2:
            s -= self._wrap
        elif s < slot - self._wrap // 2:
            s += self._wrap
        return s

    def _sfn_slot(self, slot_tx: int | None) -> str | None:
        return None if slot_tx is None else "{}.{}".format(*divmod(slot_tx, self._spf))


def _pdcch_params(p: dict[str, Any], dci_types: tuple[str, ...]) -> dict[str, Any]:
    dci = p["dci_type"]
    return {"dci": dci_types[dci] if dci < len(dci_types) else dci, "harq_id": p["harq_id"], "ndi": p["ndi"],
            "rv": p["rv"], "mcs": p["mcs"], "cce": p["cce"],
            "al": 1 << p["aggr_lvl"] if p["aggr_lvl"] <= MAX_AGGR_LVL_INDEX else None, "coreset": p["coreset_id"],
            "ss": p["ss_id"]}


def _rbs(alloc: dict[str, Any]) -> dict[str, Any]:
    rbs = alloc.get("rbs") or {}
    if rbs.get("type") == "VrbAlloc":
        return {"vrbs": f"{rbs['vrb_start']}+{rbs['vrb_length']}"}
    if rbs.get("type") == "RbgAlloc":
        return {"rbgs": f"{rbs['bitmap']:0{rbs['nof_rbgs']}b}"}
    return {}


def _syms(alloc: dict[str, Any]) -> str:
    return f"{alloc.get('sym_start', 0)}+{alloc.get('sym_length', 0)}"
