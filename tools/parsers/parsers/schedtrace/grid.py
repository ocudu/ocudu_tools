# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Resource grid occupancy of a schedtrace: the allocations of each slot as rectangles of symbols and CRBs, and their
occupancy of groups of CRBs.

Allocations are placed as the trace allows: VRBs map to PRBs one to one (non-interleaved), PRBs of a BWP to CRBs from
the BWP start, and RBGs have the size of TS 38.214 Table 5.1.2.2.1-1 configuration 1 for the size of the BWP. The VRBs
of PDSCHs of DCI format 1_0 in a common search space (SIB, RAR, paging, fallback) count from the lowest CRB of the
CORESET of their PDCCH, TS 38.214 5.1.2.2.2, those of the other PDSCHs from the BWP start. PDCCHs take the REG bundles
of their CCEs in their CORESET, TS 38.211 7.3.2.2, from the first monitoring symbol of their search space; those of an
aggregation level or CCEs out of range of their CORESET, as of a corrupt event, are left out. PRACH
occasions take the RBs of TS 38.211 Table 6.3.3.2-1 from msg1-FrequencyStart and the duration of their format; when the
PRACH and PUSCH subcarrier spacings differ their symbols are scaled, the start symbol of long formats from 15 kHz,
TS 38.211 5.3.2, and long formats last past their slot, though occupancy() counts only their first slot. CSI-RS
wider than the DL BWP spans the BWP, TS 38.331 CSI-FrequencyOccupation. SRS is left out. The UL allocations of a slot
decision are those transmitted in its slot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

NOF_SYMBOLS = 14
# Channels of the allocations, DL then UL.
DL_CHANNELS = ("PDSCH", "SIB", "RAR", "Paging", "SSB", "CSI-RS", "PDCCH")
UL_CHANNELS = ("PUSCH", "PUCCH", "PRACH")
CHANNELS = DL_CHANNELS + UL_CHANNELS
# Slot duration of each subcarrier spacing, in seconds.
SLOT_DURATION_S = {"Khz15": 1e-3, "Khz30": 0.5e-3, "Khz60": 0.25e-3, "Khz120": 0.125e-3, "Khz240": 0.0625e-3}
# Subcarrier spacing of each SubcarrierSpacing value, in kHz.
SCS_KHZ = {"Khz15": 15, "Khz30": 30, "Khz60": 60, "Khz120": 120, "Khz240": 240}
# Prach.format values, as prach_format_type of OCUDU; the first 4 are long formats.
PRACH_FORMATS = ("0", "1", "2", "3", "A1", "A2", "A3", "B1", "B4", "C0", "C2", "A1/B1", "A2/B2", "A3/B3")
# Duration of a short PRACH occasion in symbols of the PRACH subcarrier spacing, TS 38.211 Tables 6.3.3.2-2 to 4, and
# of a long one in ms, as it occupies its subframes.
_PRACH_SHORT_SYMBOLS = {"A1": 2, "B1": 2, "A1/B1": 2, "C0": 2, "A2": 4, "A2/B2": 4, "A3": 6, "A3/B3": 6, "C2": 6, "B4": 12}
_PRACH_LONG_MS = {"0": 1, "1": 3, "2": 3.5, "3": 1}
# RBs of a PRACH occasion in PUSCH RBs, by PRACH and PUSCH subcarrier spacing in kHz, TS 38.211 Table 6.3.3.2-1.
_PRACH_RBS = {(1.25, 15): 6, (1.25, 30): 3, (1.25, 60): 2, (5, 15): 24, (5, 30): 12, (5, 60): 6, (15, 15): 12,
              (15, 30): 6, (15, 60): 3, (30, 15): 24, (30, 30): 12, (30, 60): 6, (60, 60): 12, (60, 120): 6,
              (120, 60): 24, (120, 120): 12}
# RBs of the widest carrier, TS 38.101-1 Table 5.3.2-1.
MAX_NOF_RBS = 275
# Highest DlPdcch.aggr_lvl and UlPdcch.aggr_lvl, the index of aggregation level 16.
MAX_AGGR_LVL_INDEX = 4
# DlPdcch.dci_type values, as dci_dl_rnti_config_type of OCUDU; all but C_RNTI_F1_1 are DCI format 1_0.
SI_F1_0, RA_F1_0, C_RNTI_F1_0, TC_RNTI_F1_0, P_RNTI_F1_0, C_RNTI_F1_1 = range(6)


@dataclass
class Cell:
    """What placing the allocations of a cell needs, from its start event. BWPs have "cp", "scs", "crb_start" and
    "crb_length".
    """

    dl_bwp: dict[str, Any]
    ul_bwp: dict[str, Any]
    pucch_resources: list[dict[str, Any]] = field(default_factory=list)
    # Coreset and SearchSpace tables of the DL BWP.
    coresets: list[dict[str, Any]] = field(default_factory=list)
    search_spaces: list[dict[str, Any]] = field(default_factory=list)
    # PrachConfig table of the UL BWP, None without RACH.
    prach: dict[str, Any] | None = None

    @property
    def nof_crbs(self) -> int:
        """CRBs up to the end of the widest BWP."""
        return max(b["crb_start"] + b["crb_length"] for b in (self.dl_bwp, self.ul_bwp))

    @property
    def slot_duration_s(self) -> float:
        return SLOT_DURATION_S.get(self.dl_bwp["scs"], 1e-3)


def cell_of(start_event: dict[str, Any]) -> Cell:
    """The cell of a CellStartEvent of schedtrace.read()."""
    dl, ul = start_event["init_dl_bwp"], start_event["init_ul_bwp"]
    return Cell(_bwp(dl["generic_params"]), _bwp(ul["generic_params"]), ul.get("pucch_resources") or [],
                dl.get("coresets") or [], dl.get("search_spaces") or [], ul.get("prach"))


def _bwp(params: dict[str, Any]) -> dict[str, Any]:
    crbs = params["crbs"]
    return {"cp": params["cp"], "scs": params["scs"], "crb_start": crbs["start"], "crb_length": crbs["length"]}


def rbg_size(nof_prbs: int) -> int:
    """Nominal RBG size of a BWP of nof_prbs PRBs, TS 38.214 Table 5.1.2.2.1-1, configuration 1."""
    return 2 if nof_prbs <= 36 else 4 if nof_prbs <= 72 else 8 if nof_prbs <= 144 else 16


def allocations(slot_event: dict[str, Any], cell: Cell) -> list[dict[str, Any]]:
    """The allocations of a CellSlotEvent of schedtrace.read() as rectangles: "dir" ("DL" or "UL"), "channel",
    "sym_start", "sym_length", "crb_start", "crb_length", "item", the list of the decision and the index in it of the
    allocation, e.g. ["ue_pdschs", 3], and "rnti" and the details of the grant when it has them. A frequency-hopping
    allocation gives one rectangle per hop.
    """
    decision = slot_event.get("decision") or {}
    out: list[dict[str, Any]] = []

    def add(direction: str, channel: str, sym_start: int, sym_length: int, crbs: list[tuple[int, int]], info: dict[str, Any]) -> None:
        for crb_start, crb_length in crbs:
            if crb_length > 0 and sym_length > 0:
                out.append({"dir": direction, "channel": channel, "sym_start": sym_start, "sym_length": sym_length,
                            "crb_start": crb_start, "crb_length": crb_length, **info})

    def shared(direction: str, channel: str, alloc: dict[str, Any], bwp: dict[str, Any], info: dict[str, Any],
               vrb_origin: int | None = None) -> None:
        rbs = alloc.get("rbs") or {}
        sym_start, sym_length = alloc.get("sym_start", 0), alloc.get("sym_length", 0)
        if rbs.get("type") == "VrbAlloc":
            origin = bwp["crb_start"] if vrb_origin is None else vrb_origin
            first = (origin + rbs["vrb_start"], rbs["vrb_length"])
            hop = rbs.get("vrb_second_hop_start")
            if hop is None:
                add(direction, channel, sym_start, sym_length, [first], info)
            else:
                half = sym_length // 2
                add(direction, channel, sym_start, half, [first], info)
                add(direction, channel, sym_start + half, sym_length - half, [(origin + hop, rbs["vrb_length"])], info)
        elif rbs.get("type") == "RbgAlloc":
            add(direction, channel, sym_start, sym_length, rbg_crbs(rbs["bitmap"], rbs["nof_rbgs"], bwp), info)

    dl, ul = cell.dl_bwp, cell.ul_bwp
    origins = _VrbOrigins(decision.get("dl_pdcchs") or [], cell)
    for i, p in enumerate(decision.get("ue_pdschs") or []):
        cw = (p.get("codewords") or [{}])[0]
        shared("DL", "PDSCH", p, dl, {"item": ["ue_pdschs", i], "rnti": p["rnti"], "harq_id": p["harq_id"], "mcs": cw.get("mcs_index"),
                                      "tbs": cw.get("tb_size_bytes"), "new_data": cw.get("new_data"),
                                      "nof_retxs": p["nof_retxs"], "k1": p["k1"], "ue_index": p["ue_index"]},
               origins.of_rnti(p["rnti"], (C_RNTI_F1_0, TC_RNTI_F1_0, C_RNTI_F1_1)))
    for i, p in enumerate(decision.get("sib_pdschs") or []):
        shared("DL", "SIB", p, dl, {"item": ["sib_pdschs", i], "sib1": p["is_sib1"]}, origins.nth(SI_F1_0, i))
    for i, p in enumerate(decision.get("rar_pdschs") or []):
        shared("DL", "RAR", p, dl, {"item": ["rar_pdschs", i], "rnti": p["rnti"], "grants": len(p.get("grants") or [])},
               origins.of_rnti(p["rnti"], (RA_F1_0,), common=True))
    for i, p in enumerate(decision.get("paging_pdschs") or []):
        shared("DL", "Paging", p, dl, {"item": ["paging_pdschs", i], "paging_ues": len(p.get("paging_ues") or [])},
               origins.nth(P_RNTI_F1_0, i))
    for i, s in enumerate(decision.get("ssbs") or []):
        add("DL", "SSB", s["sym_start"], s["sym_length"], [(s["crbs"]["start"], s["crbs"]["length"])], {"item": ["ssbs", i], "ssb_index": s["ssb_index"]})
    for i, c in enumerate(decision.get("csi_rs") or []):
        # CSI-RS wider than the BWP spans the BWP, TS 38.331 CSI-FrequencyOccupation.
        lo = max(c["crbs"]["start"], dl["crb_start"])
        hi = min(c["crbs"]["start"] + c["crbs"]["length"], dl["crb_start"] + dl["crb_length"])
        add("DL", "CSI-RS", c["symbol0"], 1, [(lo, hi - lo)], {"item": ["csi_rs", i], "row": c["row"]})
    for i, p in enumerate(decision.get("puschs") or []):
        shared("UL", "PUSCH", p, ul, {"item": ["puschs", i], "rnti": p["rnti"], "harq_id": p["harq_id"], "tbs": p["tb_size_bytes"],
                                      "rv": p["rv_index"], "nof_retxs": p["nof_retxs"], "k2": p["k2"], "ue_index": p["ue_index"]})
    for i, p in enumerate(decision.get("pucchs") or []):
        res = cell.pucch_resources[p["res_idx"]] if p["res_idx"] < len(cell.pucch_resources) else None
        if res is None:
            continue
        nof_prbs = (res.get("format") or {}).get("nof_prbs") or 1
        info = {"item": ["pucchs", i], "rnti": p["rnti"], "format": (res.get("format") or {}).get("type"), "harq_bits": p["nof_bits_harq_ack"],
                "sr_bits": p["nof_bits_sr"], "csi_bits": p["nof_bits_csi1"]}
        start = ul["crb_start"] + res["prb_start"]
        hop = res.get("prb_second_hop_start")
        if hop is None:
            add("UL", "PUCCH", res["sym_start"], res["sym_length"], [(start, nof_prbs)], info)
        else:
            half = res["sym_length"] // 2
            add("UL", "PUCCH", res["sym_start"], half, [(start, nof_prbs)], info)
            add("UL", "PUCCH", res["sym_start"] + half, res["sym_length"] - half, [(ul["crb_start"] + hop, nof_prbs)], info)
    coresets = {cs["id"]: cs for cs in cell.coresets}
    search_spaces = {ss["id"]: ss for ss in cell.search_spaces}
    # PDCCHs repeat few CCE positions, so their CRBs are kept with the cell, outside its fields.
    pdcch_cache = cell.__dict__.setdefault("_pdcch_crbs", {})
    for key in ("dl_pdcchs", "ul_pdcchs"):
        for i, p in enumerate(decision.get(key) or []):
            cs, ss = coresets.get(p["coreset_id"]), search_spaces.get(p["ss_id"])
            nof_cces = pdcch_nof_cces(p, cs)
            if nof_cces is None or ss is None:
                continue
            info = {"item": [key, i], "rnti": p["rnti"], "dci_type": p["dci_type"], "coreset_id": p["coreset_id"],
                    "ss_id": p["ss_id"], "cce": p["cce"], "nof_cces": nof_cces}
            cce_key = (p["coreset_id"], p["cce"], nof_cces)
            crbs = pdcch_cache.get(cce_key)
            if crbs is None:
                crbs = pdcch_cache[cce_key] = pdcch_crbs(cs, p["cce"], nof_cces)
            add("DL", "PDCCH", _first_symbol(ss), cs["duration"], crbs, info)
    for i, p in enumerate(decision.get("prachs") or []):
        _add_prach(add, i, p, cell)
    return out


def _add_prach(add: Callable[..., None], index: int, prach: dict[str, Any], cell: Cell) -> None:
    """Adds the occasions of a Prach of a slot decision, one rectangle per time occasion of its frequency occasion.
    OCUDU writes one Prach per frequency occasion, index_fd_ra, of the nof_fd_ra (msg1-FDM) of the cell.
    """
    if cell.prach is None or prach["format"] >= len(PRACH_FORMATS):
        return
    fmt = PRACH_FORMATS[prach["format"]]
    pusch_khz = SCS_KHZ.get(cell.ul_bwp["scs"], 15)
    if fmt in _PRACH_LONG_MS:
        prach_khz = 5 if fmt == "3" else 1.25
        # The start symbol counts symbols of 15 kHz, TS 38.211 5.3.2; its PUSCH symbol may fall in a later slot of the
        # subframe, the slot of the Prach.
        start = round(prach["start_symbol"] * pusch_khz / 15) % NOF_SYMBOLS
        length = round(_PRACH_LONG_MS[fmt] * NOF_SYMBOLS * pusch_khz / 15)
        times = [(start, length)]
    else:
        prach_khz = SCS_KHZ.get(cell.prach.get("scs"), pusch_khz)
        scale = pusch_khz / prach_khz
        length = round(_PRACH_SHORT_SYMBOLS[fmt] * scale)
        start = round(prach["start_symbol"] * scale)
        times = [(start + t * length, length) for t in range(prach["nof_occasions"])]
    nof_rbs = _PRACH_RBS.get((prach_khz, pusch_khz))
    if nof_rbs is None:
        return
    first = cell.ul_bwp["crb_start"] + cell.prach["frequency_start"]
    fd = prach["index_fd_ra"]
    for t, (sym_start, sym_length) in enumerate(times):
        info = {"item": ["prachs", index], "format": fmt, "occasion": t, "fd_ra": fd}
        add("UL", "PRACH", sym_start, sym_length, [(first + fd * nof_rbs, nof_rbs)], info)


def pdcch_nof_cces(pdcch: dict[str, Any], coreset: dict[str, Any] | None) -> int | None:
    """CCEs of a DlPdcch or UlPdcch of a slot decision in its Coreset of the cell start event, None when the CORESET is
    unknown or invalid, or the aggregation level or CCEs out of range, as of a corrupt event.
    """
    if coreset is None or not 0 <= pdcch["aggr_lvl"] <= MAX_AGGR_LVL_INDEX or coreset_nof_cces(coreset) == 0:
        return None
    # The aggregation level is written as its index: 0 is 1 CCE, 4 is 16 CCEs.
    nof_cces = 1 << pdcch["aggr_lvl"]
    return nof_cces if pdcch["cce"] + nof_cces <= coreset_nof_cces(coreset) else None


def coreset_nof_cces(coreset: dict[str, Any]) -> int:
    """CCEs of a Coreset of the cell start event, 0 when its duration, RBs or interleaving parameters are invalid."""
    nof_symbols = coreset["duration"]
    interleaving = coreset.get("interleaving")
    if not 1 <= nof_symbols <= 3:
        return 0
    nof_rbs = sum(r["length"] for r in coreset["crbs"])
    # A CORESET lies in a BWP, of at most 275 RBs.
    if nof_rbs > MAX_NOF_RBS:
        return 0
    nof_regs = nof_rbs * nof_symbols
    if interleaving:
        bundle_size, size = interleaving["reg_bundle_size"], interleaving["interleaver_size"]
        if bundle_size not in (2, 3, 6) or size not in (2, 3, 6) or bundle_size % nof_symbols or \
                nof_regs % (bundle_size * size):
            return 0
    return nof_regs // 6


def pdcch_crbs(coreset: dict[str, Any], cce: int, nof_cces: int) -> list[tuple[int, int]]:
    """CRB ranges of the CCEs [cce, cce + nof_cces) of a Coreset of the cell start event, merged where contiguous, with
    the CCE-to-REG mapping of TS 38.211 7.3.2.2. CCEs past those of coreset_nof_cces() hold no CRBs.
    """
    nof_cces = max(0, min(cce + nof_cces, coreset_nof_cces(coreset)) - cce)
    crbs = [c for r in coreset["crbs"] for c in range(r["start"], r["start"] + r["length"])]
    nof_symbols = coreset["duration"]
    nof_regs = len(crbs) * nof_symbols
    interleaving = coreset.get("interleaving")
    bundle_size = interleaving["reg_bundle_size"] if interleaving else 6
    nof_bundles = nof_regs // bundle_size
    if interleaving:
        size = interleaving["interleaver_size"]
        nof_cols = nof_regs // (bundle_size * size)

        def bundle_of(x: int) -> int:
            c, r = divmod(x, size)
            return (r * nof_cols + c + interleaving["shift_index"]) % nof_bundles
    else:
        def bundle_of(x: int) -> int:
            return x

    per_cce = 6 // bundle_size
    rbs = set()
    for j in range(cce, cce + nof_cces):
        for x in range(j * per_cce, (j + 1) * per_cce):
            # REGs are numbered first in time, so a bundle spans all the symbols of the CORESET.
            bundle = bundle_of(x)
            first = bundle * bundle_size // nof_symbols
            rbs.update(range(first, first + bundle_size // nof_symbols))
    ranges: list[tuple[int, int]] = []
    for crb in sorted(crbs[i] for i in rbs if i < len(crbs)):
        if ranges and ranges[-1][0] + ranges[-1][1] == crb:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + 1)
        else:
            ranges.append((crb, 1))
    return ranges


def _first_symbol(search_space: dict[str, Any]) -> int:
    """First monitoring symbol of a SearchSpace of the cell start event, of its first entry for SearchSpace#0."""
    bitmap = (search_space.get("monitoring_symbols") or [1])[0]
    return (bitmap & -bitmap).bit_length() - 1 if bitmap else 0


class _VrbOrigins:
    """The CRBs from which the VRBs of the PDSCHs of a slot count, from the DL PDCCHs that schedule them."""

    def __init__(self, pdcchs: list[dict[str, Any]], cell: Cell):
        self.pdcchs = pdcchs
        self.lowest_crb = {cs["id"]: min(r["start"] for r in cs["crbs"]) for cs in cell.coresets if cs.get("crbs")}
        self.common = {ss["id"]: ss["is_common"] for ss in cell.search_spaces}

    def of_rnti(self, rnti: int, dci_types: tuple[int, ...], common: bool = False) -> int | None:
        """The origin of the PDSCH of the PDCCH of an RNTI and of one of dci_types. common: of a common PDSCH."""
        pdcch = next((p for p in self.pdcchs if p["rnti"] == rnti and p["dci_type"] in dci_types), None)
        return self._of(pdcch, common)

    def nth(self, dci_type: int, n: int) -> int | None:
        """The origin of the n-th common PDSCH of a DCI type, scheduled in the order of their PDCCHs."""
        pdcchs = [p for p in self.pdcchs if p["dci_type"] == dci_type]
        return self._of(pdcchs[n] if n < len(pdcchs) else None, True)

    def _of(self, pdcch: dict[str, Any] | None, common: bool) -> int | None:
        """The lowest CRB of the CORESET of a PDCCH of DCI format 1_0 in a common search space, else None for the BWP
        start. A common PDSCH whose PDCCH is not in the slot is taken as scheduled from CORESET#0.
        """
        if pdcch is None:
            return self.lowest_crb.get(0) if common else None
        if pdcch["dci_type"] == C_RNTI_F1_1 or not self.common.get(pdcch["ss_id"], False):
            return None
        return self.lowest_crb.get(pdcch["coreset_id"])


def rbg_crbs(bitmap: int, nof_rbgs: int, bwp: dict[str, Any]) -> list[tuple[int, int]]:
    """CRB ranges of the RBGs of a bitmap, merged where contiguous. RBG 0 is the most significant of the nof_rbgs bits,
    as OCUDU writes them.
    """
    size = rbg_size(bwp["crb_length"])
    start, end = bwp["crb_start"], bwp["crb_start"] + bwp["crb_length"]
    # The first RBG ends at the next multiple of the RBG size, counted in CRBs.
    first_end = (start // size + 1) * size
    ranges: list[tuple[int, int]] = []
    for i in range(nof_rbgs):
        if not (bitmap >> (nof_rbgs - 1 - i)) & 1:
            continue
        lo = start if i == 0 else first_end + (i - 1) * size
        hi = min(end, first_end if i == 0 else lo + size)
        # RBGs past the BWP, of a bitmap of another RBG size configuration or a corrupt one, hold no CRBs.
        if hi <= lo:
            continue
        if ranges and ranges[-1][0] + ranges[-1][1] == lo:
            ranges[-1] = (ranges[-1][0], hi - ranges[-1][0])
        else:
            ranges.append((lo, hi - lo))
    return ranges


def occupancy(rects: list[dict[str, Any]], group_size: int) -> dict[tuple[int, str], float]:
    """Resource elements of the rectangles of a slot in each group of group_size CRBs, by (group, channel), as a
    fraction of the group's elements in a slot.
    """
    out: dict[tuple[int, str], float] = {}
    for r in rects:
        lo, hi = r["crb_start"], r["crb_start"] + r["crb_length"]
        for g in range(lo // group_size, (hi - 1) // group_size + 1):
            overlap = min(hi, (g + 1) * group_size) - max(lo, g * group_size)
            key = (g, r["channel"])
            # Allocations that last past their slot, as long PRACH formats, count only in it.
            nof_symbols = min(r["sym_start"] + r["sym_length"], NOF_SYMBOLS) - r["sym_start"]
            out[key] = out.get(key, 0.0) + overlap * nof_symbols / (group_size * NOF_SYMBOLS)
    return out
