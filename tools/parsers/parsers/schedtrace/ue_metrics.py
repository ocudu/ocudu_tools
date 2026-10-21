# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Per-UE metrics of a schedtrace: the grants and reports of each UE in a slot, and their aggregates per bin of time,
named as the fields of the METRICS "Scheduler UE" lines of OCUDU where they mean the same.

The trace has no UL CRC, buffer status or power headroom, so UL rates are of the scheduled TBS of new transmissions,
not of the decoded ones; ul_layers is the mean layers of the PUSCHs, not the UL RI of SRS. dl_nof_nok counts the
HARQ-ACK reports of NACK and of DTX, as METRICS does, dl_nof_dtx the latter alone. HARQ processes that time out
without a report, and those without feedback, e.g. of NTN, have no trace event, so they count in neither dl_nof_ok nor
dl_nof_nok, unlike in METRICS. cqi and dl_ri are means over the reports of a bin, and dl_mcs the mean over the codewords
of the PDSCHs of a bin, all unrounded, so that means over bins are not biased; METRICS prints cqi and dl_mcs rounded half
away from zero, as round_half_up() rounds them. Msg3 PUSCHs, of UEs without a DU UE index yet, count under their
TC-RNTI with a ue_index of None, which a later grant or report of the RNTI fills in.
"""

from __future__ import annotations

from typing import Any

from ..ran.rnti import normalize
from .grid import Cell, rbg_crbs

# Default duration of an aggregation bin, in seconds.
BIN_S = 0.01
# Fields of the rows of each kind of grant or report of slot_items(), all with "rnti" and "ue_index" first.
ITEM_FIELDS: dict[str, tuple[str, ...]] = {
    "dl_grants": ("rnti", "ue_index", "harq_id", "mcs", "tbs", "nof_prbs", "new_data", "nof_retxs", "k1", "mcs2"),
    "ul_grants": ("rnti", "ue_index", "harq_id", "tbs", "nof_prbs", "nof_layers", "rv", "nof_retxs", "k2"),
    "harq_acks": ("rnti", "ue_index", "harq_id", "status", "tbs"),
    "csi_reports": ("rnti", "ue_index", "cqi", "ri", "rsrp"),
}
# Units of the fields of slot_items() and of the aggregates.
ITEM_UNITS = {"tbs": "B", "rsrp": "dBm"}
UE_FIELDS = ("rnti", "ue_index", "dl_brate", "dl_sched_brate", "dl_mcs", "dl_nof_prbs", "dl_nof_newtx", "dl_nof_retx",
             "dl_nof_ok", "dl_nof_nok", "dl_nof_dtx", "dl_error_rate", "ul_sched_brate", "ul_nof_prbs", "ul_nof_newtx",
             "ul_nof_retx", "ul_layers", "cqi", "dl_ri", "rsrp", "sr_count")
UE_UNITS = {"dl_brate": "bps", "dl_sched_brate": "bps", "ul_sched_brate": "bps", "dl_error_rate": "%", "rsrp": "dBm"}
CELL_FIELDS = ("latency_avg", "latency_max", "nof_slots", "dl_nof_prbs", "ul_nof_prbs", "dl_nof_ues", "ul_nof_ues",
               "failed_attempts")
CELL_UNITS = {"latency_avg": "us", "latency_max": "us"}
# OCUDU MAX_NOF_DU_UES, from which DU UE indexes are invalid, e.g. INVALID_DU_UE_INDEX of Msg3 PUSCHs.
MAX_NOF_DU_UES = 8192
_STATUS = ("Nack", "Ack", "Dtx")


def round_half_up(value: float | None) -> int | None:
    """A non-negative value rounded as std::round, unlike round(), which rounds halves to even; None as is."""
    return None if value is None else int(value + 0.5)


def ue_index(value: int | None) -> int | None:
    """A DU UE index of the trace, or None if it is invalid."""
    return value if value is not None and 0 <= value < MAX_NOF_DU_UES else None


def nof_prbs(rbs: dict[str, Any] | None, bwp: dict[str, Any]) -> int:
    """PRBs of the RbAlloc of a grant in a BWP of grid.Cell."""
    if not rbs:
        return 0
    if rbs.get("type") == "VrbAlloc":
        return rbs["vrb_length"]
    if rbs.get("type") == "RbgAlloc":
        return sum(n for _, n in rbg_crbs(rbs["bitmap"], rbs["nof_rbgs"], bwp))
    return 0


def slot_items(event: dict[str, Any], cell: Cell) -> dict[str, list[tuple]]:
    """The grants and reports of the UEs of a CellSlotEvent of schedtrace.read(), as rows of the fields of
    ITEM_FIELDS by kind, RNTIs as OCUDU logs print them and invalid UE indexes as None, see ue_index(). A DL grant has
    the MCS and new data flag of its first codeword, the MCS of its second codeword as mcs2, None without one, and the
    TBS of all its codewords.
    """
    decision = event.get("decision") or {}
    dl = []
    for p in decision.get("ue_pdschs") or []:
        cws = p.get("codewords") or [{}]
        cw = cws[0]
        sizes = [c["tb_size_bytes"] for c in cws if c.get("tb_size_bytes") is not None]
        mcs2 = cws[1].get("mcs_index") if len(cws) > 1 else None
        dl.append((normalize(p["rnti"]), ue_index(p["ue_index"]), p["harq_id"], cw.get("mcs_index"),
                   sum(sizes) if sizes else None, nof_prbs(p.get("rbs"), cell.dl_bwp), cw.get("new_data"),
                   p["nof_retxs"], p["k1"], mcs2))
    ul = [(normalize(p["rnti"]), ue_index(p["ue_index"]), p["harq_id"], p["tb_size_bytes"],
           nof_prbs(p.get("rbs"), cell.ul_bwp), p.get("nof_layers"), p["rv_index"], p["nof_retxs"], p["k2"])
          for p in decision.get("puschs") or []]
    acks, csis, srs = [], [], []
    for i in event.get("inputs") or []:
        kind = i and i.get("type")
        if kind == "HarqAckEvent":
            status = i["status"]
            if not isinstance(status, str):
                # A status of a newer schema than the decoder's, given as its value.
                status = _STATUS[status] if 0 <= status < len(_STATUS) else f"Status{status}"
            acks.append((normalize(i["rnti"]), ue_index(i["ue_idx"]), i["harq_id"], status, i["tbs"]))
        elif kind == "CsiReportEvent":
            rsrp = i.get("rsrp_dbm") or []
            csis.append((normalize(i["rnti"]), ue_index(i["ue_idx"]), i.get("first_tb_wb_cqi"), i.get("ri"),
                         rsrp[0] if rsrp else None))
        elif kind == "SrEvent":
            srs.append((normalize(i["rnti"]), ue_index(i["ue_idx"])))
    return {"dl_grants": dl, "ul_grants": ul, "harq_acks": acks, "csi_reports": csis, "srs": srs}


class Aggregator:
    """Sums of the grants and reports of each UE, and of each cell slot, per bin of bin_s seconds. Aggregators of parts
    of a trace, with the same bin, merge into the one of the whole.
    """

    def __init__(self, bin_s: float = BIN_S) -> None:
        # Bin size in microseconds, so that bins are exact, and in seconds, as rounded to it.
        self._bin_us = max(1, round(bin_s * 1e6))
        self.bin_s = self._bin_us / 1e6
        # Sums of each (bin, RNTI), and of each bin for the cell, with the first record of the bin.
        self.ues: dict[tuple[int, str], dict[str, Any]] = {}
        self.cells: dict[int, dict[str, Any]] = {}

    def add(self, record: int, event: dict[str, Any], items: dict[str, list[tuple]]) -> None:
        """Adds a CellSlotEvent of record number record and its slot_items()."""
        b = event["timestamp_us"] // self._bin_us
        latency = event.get("latency_us") or 0
        c = self.cells.get(b)
        if c is None:
            c = self.cells[b] = {"record": record, "n": 0, "latency_sum": 0, "latency_max": latency, "dl_nof_prbs": 0,
                                 "ul_nof_prbs": 0, "dl_ues": set(), "ul_ues": set(), "failed_attempts": 0}
        c["record"] = min(c["record"], record)
        c["n"] += 1
        c["latency_sum"] += latency
        c["latency_max"] = max(c["latency_max"], latency)
        failed = (event.get("decision") or {}).get("failed_attempts") or {}
        c["failed_attempts"] += sum(v for v in failed.values() if isinstance(v, int))
        for rnti, ue, _, mcs, tbs, prbs, new_data, _, _, mcs2 in items["dl_grants"]:
            u = self._ue(b, rnti, ue, record)
            if new_data:
                u["dl_nof_newtx"] += 1
                u["dl_newtx_bytes"] += tbs or 0
            else:
                u["dl_nof_retx"] += 1
            # The mean of every codeword, as METRICS.
            for cw_mcs in (mcs, mcs2):
                if cw_mcs is not None:
                    u["dl_mcs_sum"] += cw_mcs
                    u["dl_mcs_n"] += 1
            u["dl_nof_prbs"] += prbs
            c["dl_nof_prbs"] += prbs
            c["dl_ues"].add(rnti)
        for rnti, ue, _, tbs, prbs, layers, _, nof_retxs, _ in items["ul_grants"]:
            u = self._ue(b, rnti, ue, record)
            if nof_retxs:
                u["ul_nof_retx"] += 1
            else:
                u["ul_nof_newtx"] += 1
                u["ul_newtx_bytes"] += tbs or 0
            if layers:
                u["ul_layers_sum"] += layers
                u["ul_layers_n"] += 1
            u["ul_nof_prbs"] += prbs
            c["ul_nof_prbs"] += prbs
            c["ul_ues"].add(rnti)
        for rnti, ue, _, status, tbs in items["harq_acks"]:
            u = self._ue(b, rnti, ue, record)
            if status == "Ack":
                u["dl_nof_ok"] += 1
                u["dl_ok_bytes"] += tbs or 0
            elif status in ("Nack", "Dtx"):
                u["dl_nof_nok"] += 1
                u["dl_nof_dtx"] += status == "Dtx"
        for rnti, ue, cqi, ri, rsrp in items["csi_reports"]:
            u = self._ue(b, rnti, ue, record)
            if cqi is not None:
                u["cqi_sum"] += cqi
                u["cqi_n"] += 1
            if ri is not None:
                u["ri_sum"] += ri
                u["ri_n"] += 1
            if rsrp is not None:
                u["rsrp_sum"] += rsrp
                u["rsrp_n"] += 1
        for rnti, ue in items["srs"]:
            self._ue(b, rnti, ue, record)["sr_count"] += 1

    def merge(self, other: Aggregator) -> None:
        """Adds the sums of an aggregator of a later part of the trace, of the same bin size."""
        if other._bin_us != self._bin_us:
            raise ValueError(f"Cannot merge bins of {other.bin_s} s into bins of {self.bin_s} s.")
        for key, o in other.ues.items():
            u = self.ues.get(key)
            if u is None:
                self.ues[key] = o
                continue
            for k, v in o.items():
                if k == "record":
                    u[k] = min(u[k], v)
                elif k == "ue_index":
                    u[k] = v if v is not None else u[k]
                else:
                    u[k] += v
        for b, o in other.cells.items():
            c = self.cells.get(b)
            if c is None:
                self.cells[b] = o
                continue
            c["record"] = min(c["record"], o["record"])
            c["latency_max"] = max(c["latency_max"], o["latency_max"])
            c["dl_ues"] |= o["dl_ues"]
            c["ul_ues"] |= o["ul_ues"]
            for k in ("n", "latency_sum", "dl_nof_prbs", "ul_nof_prbs", "failed_attempts"):
                c[k] += o[k]

    def ue_rows(self) -> list[tuple]:
        """Rows of time, record and the values of UE_FIELDS, per bin and RNTI, in time order."""
        return [(b * self.bin_s, u["record"], *_ue_values(rnti, u, self.bin_s)) for (b, rnti), u in sorted(self.ues.items())]

    def ue_totals(self, duration_s: float) -> list[tuple]:
        """Rows of the first and last bin time and the values of UE_FIELDS over all the bins of each RNTI, rates over
        duration_s seconds, e.g. of the whole trace, in RNTI order.
        """
        totals: dict[str, dict[str, Any]] = {}
        spans: dict[str, list[int]] = {}
        for (b, rnti), u in sorted(self.ues.items()):
            t = totals.get(rnti)
            if t is None:
                totals[rnti], spans[rnti] = dict(u), [b, b]
                continue
            spans[rnti][1] = b
            for k, v in u.items():
                if k == "ue_index":
                    t[k] = v if v is not None else t[k]
                elif k != "record":
                    t[k] += v
        return [(spans[rnti][0] * self.bin_s, spans[rnti][1] * self.bin_s, *_ue_values(rnti, t, duration_s))
                for rnti, t in sorted(totals.items())]

    def cell_rows(self) -> list[tuple]:
        """Rows of time, record and the values of CELL_FIELDS, per bin, in time order."""
        return [(b * self.bin_s, c["record"], c["latency_sum"] / c["n"], c["latency_max"], c["n"], c["dl_nof_prbs"],
                 c["ul_nof_prbs"], len(c["dl_ues"]), len(c["ul_ues"]), c["failed_attempts"])
                for b, c in sorted(self.cells.items())]

    def _ue(self, b: int, rnti: str, ue_index: int | None, record: int) -> dict[str, Any]:
        u = self.ues.get((b, rnti))
        if u is None:
            u = self.ues[(b, rnti)] = dict.fromkeys(_UE_SUMS, 0)
            u.update(record=record, ue_index=ue_index)
        # A Msg3 PUSCH has no index, which the later grants and reports of its UE in the bin give.
        if u["ue_index"] is None:
            u["ue_index"] = ue_index
        u["record"] = min(u["record"], record)
        return u


def _ue_values(rnti: str, u: dict[str, Any], seconds: float) -> tuple:
    """The values of UE_FIELDS from the sums of a UE over a span of seconds."""
    reports = u["dl_nof_ok"] + u["dl_nof_nok"]
    return (
        rnti, u["ue_index"], u["dl_ok_bytes"] * 8 / seconds, u["dl_newtx_bytes"] * 8 / seconds,
        u["dl_mcs_sum"] / u["dl_mcs_n"] if u["dl_mcs_n"] else None,
        u["dl_nof_prbs"], u["dl_nof_newtx"], u["dl_nof_retx"], u["dl_nof_ok"], u["dl_nof_nok"], u["dl_nof_dtx"],
        100 * (reports - u["dl_nof_ok"]) / reports if reports else None,
        u["ul_newtx_bytes"] * 8 / seconds, u["ul_nof_prbs"], u["ul_nof_newtx"], u["ul_nof_retx"],
        u["ul_layers_sum"] / u["ul_layers_n"] if u["ul_layers_n"] else None,
        u["cqi_sum"] / u["cqi_n"] if u["cqi_n"] else None, u["ri_sum"] / u["ri_n"] if u["ri_n"] else None,
        u["rsrp_sum"] / u["rsrp_n"] if u["rsrp_n"] else None, u["sr_count"],
    )


# Sums kept per bin and UE.
_UE_SUMS = ("dl_ok_bytes", "dl_newtx_bytes", "dl_mcs_sum", "dl_mcs_n", "dl_nof_prbs", "dl_nof_newtx", "dl_nof_retx",
            "dl_nof_ok", "dl_nof_nok", "dl_nof_dtx", "ul_newtx_bytes", "ul_nof_prbs", "ul_nof_newtx", "ul_nof_retx",
            "ul_layers_sum", "ul_layers_n", "cqi_sum", "cqi_n", "ri_sum", "ri_n", "rsrp_sum", "rsrp_n", "sr_count")
