# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Collisions in the decisions of the slots of a schedtrace: pairs of allocations of a slot whose resources overlap.

Kinds of collisions, in KINDS: PDSCH with PDSCH (UE, SIB, RAR and paging PDSCHs), PUSCH with PUSCH, PUSCH with
PUCCH, PDCCH with PDSCH, and PDCCH with PDCCH, whose CCEs overlap in their CORESET.

Allocations are placed as grid.allocations() places them, which leaves out PDCCHs out of range of their CORESET.
"""

from __future__ import annotations

import mmap
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from . import HEADER, event_at, offsets, start_event
from .grid import Cell, allocations, cell_of, pdcch_nof_cces

KINDS = ("PDSCH-PDSCH", "PUSCH-PUSCH", "PUSCH-PUCCH", "PDCCH-PDSCH", "PDCCH-PDCCH")
# Kind of the collision of two channels of grid.allocations(), by their pair, in both orders.
_PDSCHS = ("PDSCH", "SIB", "RAR", "Paging")
_KIND_OF = {
    **{(a, b): "PDSCH-PDSCH" for a in _PDSCHS for b in _PDSCHS},
    ("PUSCH", "PUSCH"): "PUSCH-PUSCH",
    ("PUSCH", "PUCCH"): "PUSCH-PUCCH",
    ("PUCCH", "PUSCH"): "PUSCH-PUCCH",
    **{(a, b): "PDCCH-PDSCH" for p in _PDSCHS for a, b in (("PDCCH", p), (p, "PDCCH"))},
    ("PDCCH", "PDCCH"): "PDCCH-PDCCH",
}


@dataclass
class Collision:
    """Two allocations of a slot whose resources overlap."""

    kind: str
    slot_tx: int
    timestamp_us: int
    # The allocations, as grid.allocations() gives them, without "sym_start", "sym_length", "crb_start" and
    # "crb_length" but with "rects", their (first symbol, symbols, first CRB, CRBs), one per frequency hop or CRB range;
    # or for PDCCH-PDCCH in one CORESET and monitoring symbol, PDCCHs with "item", "rnti", "dci_type", "coreset_id",
    # "cce" and "nof_cces".
    a: dict[str, Any]
    b: dict[str, Any]
    # Overlapping resources: (first symbol, symbols, first CRB, CRBs) of each pair of overlapping rects, or for PDCCHs
    # in one CORESET and monitoring symbol the (first CCE, CCEs) they share.
    overlap: list[tuple[int, ...]] = field(default_factory=list)


def slot_collisions(event: dict[str, Any], cell: Cell) -> list[Collision]:
    """The collisions of a CellSlotEvent of schedtrace.read()."""
    out: list[Collision] = []
    items = _items(allocations(event, cell))
    # First symbol of each PDCCH, that of the monitoring occasion of its search space.
    first_symbol = {tuple(i["item"]): i["rects"][0][0] for i in items if i["channel"] == "PDCCH"}
    for a, b in combinations(items, 2):
        kind = _KIND_OF.get((a["channel"], b["channel"]))
        # PDCCHs of one CORESET and monitoring occasion collide by their CCEs, below.
        if kind is None or (kind == "PDCCH-PDCCH" and a["coreset_id"] == b["coreset_id"] and
                            a["rects"][0][0] == b["rects"][0][0]):
            continue
        overlap = [o for ra in a["rects"] for rb in b["rects"] if (o := _rect_overlap(ra, rb))]
        if overlap:
            out.append(Collision(kind, event["slot_tx"], event["timestamp_us"], a, b, overlap))
    for a, b in combinations(_pdcchs(event, cell), 2):
        if a["coreset_id"] != b["coreset_id"] or first_symbol.get(tuple(a["item"])) != first_symbol.get(tuple(b["item"])):
            continue
        lo, hi = max(a["cce"], b["cce"]), min(a["cce"] + a["nof_cces"], b["cce"] + b["nof_cces"])
        if lo < hi:
            out.append(Collision("PDCCH-PDCCH", event["slot_tx"], event["timestamp_us"], a, b, [(lo, hi - lo)]))
    return out


def find(path: str | os.PathLike[str], start: int | None = None, end: int | None = None) -> Iterator[Collision]:
    """The collisions of the slots of a trace, of the events from the byte offset start, which must be the offset of an
    event, or the first one, up to end. The cell is read from the first event of the trace.
    """
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        cell = cell_of(start_event(mm, os.fspath(path)))
        # From the first event, without checking the version again.
        for offset in offsets(mm, HEADER.size if start is None else start, end):
            event = event_at(mm, offset)
            if event["type"] == "CellSlotEvent":
                yield from slot_collisions(event, cell)


def _items(allocs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The allocations of a slot, one per item of its decision, the rects of frequency hops together."""
    items: dict[tuple[str, int], dict[str, Any]] = {}
    for a in allocs:
        rect = (a["sym_start"], a["sym_length"], a["crb_start"], a["crb_length"])
        key = tuple(a["item"])
        if key in items:
            items[key]["rects"].append(rect)
            continue
        info = {k: v for k, v in a.items() if k not in ("sym_start", "sym_length", "crb_start", "crb_length")}
        items[key] = {**info, "rects": [rect]}
    return list(items.values())


def _rect_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    sym_lo, sym_hi = max(a[0], b[0]), min(a[0] + a[1], b[0] + b[1])
    crb_lo, crb_hi = max(a[2], b[2]), min(a[2] + a[3], b[2] + b[3])
    if sym_lo < sym_hi and crb_lo < crb_hi:
        return (sym_lo, sym_hi - sym_lo, crb_lo, crb_hi - crb_lo)
    return None


def _pdcchs(event: dict[str, Any], cell: Cell) -> list[dict[str, Any]]:
    """The DL and UL PDCCHs of a slot, with their CCEs, but those of grid.pdcch_nof_cces() None."""
    decision = event.get("decision") or {}
    coresets = {cs["id"]: cs for cs in cell.coresets}
    out = []
    for key in ("dl_pdcchs", "ul_pdcchs"):
        for i, p in enumerate(decision.get(key) or []):
            nof_cces = pdcch_nof_cces(p, coresets.get(p["coreset_id"]))
            if nof_cces is not None:
                out.append({"item": [key, i], "rnti": p["rnti"], "dci_type": p["dci_type"],
                            "coreset_id": p["coreset_id"], "cce": p["cce"], "nof_cces": nof_cces})
    return out
