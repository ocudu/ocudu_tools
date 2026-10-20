# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import tempfile
import unittest
import warnings
from pathlib import Path

from parsers import schedtrace
from parsers.schedtrace import collisions, grid

DATA = Path(__file__).parent / "data"


def vrbs(start, length, hop=None):
    return {"type": "VrbAlloc", "vrb_start": start, "vrb_length": length, "vrb_second_hop_start": hop}


def pdsch(rnti, rbs, sym_start=2, sym_length=12):
    return {"rnti": rnti, "rbs": rbs, "sym_start": sym_start, "sym_length": sym_length, "codewords": [], "harq_id": 0,
            "ue_index": 0, "nof_retxs": 0, "k1": 4}


def pusch(rnti, rbs, sym_start=0, sym_length=14):
    return {"rnti": rnti, "rbs": rbs, "sym_start": sym_start, "sym_length": sym_length, "harq_id": 0, "tb_size_bytes": 0,
            "rv_index": 0, "nof_retxs": 0, "k2": 4, "ue_index": 0}


def pdcch(rnti, coreset_id, cce, aggr_lvl, dci_type=grid.C_RNTI_F1_1, ss_id=2):
    return {"rnti": rnti, "dci_type": dci_type, "coreset_id": coreset_id, "ss_id": ss_id, "cce": cce, "aggr_lvl": aggr_lvl}


def common_pdsch(rbs, **fields):
    return {"rbs": rbs, "sym_start": 2, "sym_length": 12, **fields}


def slot(**decision):
    return {"type": "CellSlotEvent", "slot_tx": 7, "timestamp_us": 1, "decision": decision}


class CollisionsTest(unittest.TestCase):
    def setUp(self):
        start = next(schedtrace.read(DATA / "schedtrace_cell0.bin"))
        self.cell = grid.cell_of(start)

    def kinds(self, event):
        return [(c.kind, c.a["item"], c.b["item"], c.overlap) for c in collisions.slot_collisions(event, self.cell)]

    def test_find_warns_once_of_a_corrupt_size(self):
        data = bytearray((DATA / "schedtrace_cell0.bin").read_bytes())
        second = schedtrace.offsets(data)[1]
        data[second : second + 4] = (schedtrace.MAX_EVENT_SIZE + 1).to_bytes(4, "little")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(data)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertEqual(list(collisions.find(path)), [])
        self.assertEqual(len(caught), 1)

    def test_find_raises_the_decode_error_of_a_corrupt_event(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        first = schedtrace.HEADER.size
        size = int.from_bytes(data[first : first + 4], "little")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(data[: first + 14] + b"\xff" * (size - 10))
            with self.assertRaises(Exception) as ctx:
                list(collisions.find(path))
            self.assertNotIsInstance(ctx.exception, BufferError)

    def test_pdschs(self):
        event = slot(ue_pdschs=[pdsch(1, vrbs(0, 10)), pdsch(2, vrbs(8, 4), sym_start=10, sym_length=4), pdsch(3, vrbs(20, 5))])
        # The DL BWP starts at CRB 2: VRBs 8 and 9 are CRBs 10 and 11, in symbols 10 to 13.
        self.assertEqual(self.kinds(event), [("PDSCH-PDSCH", ["ue_pdschs", 0], ["ue_pdschs", 1], [(10, 4, 10, 2)])])

    def test_pdschs_of_dci_1_0_count_vrbs_from_their_coreset(self):
        # UE 2 is of DCI format 1_0 in common search space 1 of CORESET 1, which starts at CRB 8, the BWP at CRB 2.
        event = slot(ue_pdschs=[pdsch(1, vrbs(0, 2)), pdsch(2, vrbs(0, 2)), pdsch(3, vrbs(6, 2))],
                     dl_pdcchs=[pdcch(1, 1, 0, 2), pdcch(2, 1, 4, 2, dci_type=grid.C_RNTI_F1_0, ss_id=1), pdcch(3, 1, 8, 2)])
        self.assertEqual(self.kinds(event), [("PDSCH-PDSCH", ["ue_pdschs", 1], ["ue_pdschs", 2], [(2, 12, 8, 2)])])

    def test_puschs_and_pucchs(self):
        # PUCCH resource 1: 2 PRBs at PRB 40 in symbol 12, hopping to PRB 7 in symbol 13.
        pucch = {"rnti": 3, "res_idx": 1, "nof_bits_harq_ack": 1, "nof_bits_sr": 0, "nof_bits_csi1": 0}
        event = slot(puschs=[pusch(1, vrbs(0, 10)), pusch(2, vrbs(9, 2)), pusch(4, vrbs(48, 2))], pucchs=[pucch])
        # The UL BWP starts at CRB 0.
        self.assertEqual(self.kinds(event), [
            ("PUSCH-PUSCH", ["puschs", 0], ["puschs", 1], [(0, 14, 9, 1)]),
            ("PUSCH-PUCCH", ["puschs", 0], ["pucchs", 0], [(13, 1, 7, 2)]),
        ])

    def test_frequency_hops_of_one_allocation_do_not_collide(self):
        event = slot(puschs=[pusch(1, vrbs(0, 4, hop=2))])
        self.assertEqual(self.kinds(event), [])

    def test_pdcchs(self):
        # Aggregation level 1 is 2 CCEs and 2 is 4: 0 to 1 and 0 to 3 share 0 and 1, in CORESET 1 of 4 CCEs, where CCE 6
        # is past the CORESET. CORESET 0, of CRBs 10 to 33 in symbols 0 and 1, overlaps CORESET 1, so its PDCCH of CRBs
        # 16 to 27 shares REs with both of CORESET 1.
        event = slot(dl_pdcchs=[pdcch(1, 1, 0, 1), pdcch(2, 0, 2, 2)], ul_pdcchs=[pdcch(3, 1, 0, 2), pdcch(4, 1, 6, 0)])
        self.assertEqual(self.kinds(event), [
            ("PDCCH-PDCCH", ["dl_pdcchs", 0], ["dl_pdcchs", 1], [(0, 1, 16, 4)]),
            ("PDCCH-PDCCH", ["dl_pdcchs", 1], ["ul_pdcchs", 0], [(0, 1, 16, 4)]),
            ("PDCCH-PDCCH", ["dl_pdcchs", 0], ["ul_pdcchs", 0], [(0, 2)]),
        ])

    def test_pdcchs_of_other_monitoring_symbols(self):
        # Search space 2 of CORESET 1 monitoring symbol 7: its CCEs are those of another occasion.
        next(ss for ss in self.cell.search_spaces if ss["id"] == 2)["monitoring_symbols"] = [1 << 7]
        event = slot(dl_pdcchs=[pdcch(1, 1, 0, 2, ss_id=1), pdcch(2, 1, 0, 2, ss_id=2)])
        self.assertEqual(self.kinds(event), [])

    def test_pdcchs_and_pdschs(self):
        # CCE 1 of CORESET 1 is REG bundle 3, CRBs 38 to 43, in symbol 0 that search space 2 monitors. PDSCHs of DCI
        # 1_1 count from the BWP start, CRB 2.
        event = slot(ue_pdschs=[pdsch(1, vrbs(30, 10), sym_start=0, sym_length=14), pdsch(2, vrbs(0, 4), sym_start=1, sym_length=13)],
                     dl_pdcchs=[pdcch(3, 1, 1, 0)])
        self.assertEqual(self.kinds(event), [("PDCCH-PDSCH", ["ue_pdschs", 0], ["dl_pdcchs", 0], [(0, 1, 38, 4)])])

    def test_find(self):
        self.assertEqual(list(collisions.find(DATA / "schedtrace_cell0.bin")), [])

    def test_find_of_a_trace_without_its_start_event(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            # Just opened: the header and part of the start event.
            path.write_bytes(data[: schedtrace.HEADER.size + 20])
            with self.assertRaisesRegex(ValueError, "does not start with the start event"):
                list(collisions.find(path))


class VrbOriginTest(unittest.TestCase):
    """VRBs of PDSCHs of DCI format 1_0 in a common search space count from their CORESET, the others from the BWP."""

    def setUp(self):
        # DL BWP from CRB 2; CORESET 0 from CRB 10, CORESET 1 from CRB 8; search spaces 0 and 1 common, 2 UE-specific.
        self.cell = grid.cell_of(next(schedtrace.read(DATA / "schedtrace_cell0.bin")))

    def starts(self, **decision):
        allocs = grid.allocations(slot(**decision), self.cell)
        return {tuple(a["item"]): a["crb_start"] for a in allocs if a["channel"] != "PDCCH"}

    def test_ue_pdschs(self):
        starts = self.starts(
            ue_pdschs=[pdsch(1, vrbs(0, 2)), pdsch(2, vrbs(0, 2)), pdsch(3, vrbs(0, 2)), pdsch(4, vrbs(0, 2))],
            dl_pdcchs=[pdcch(1, 1, 0, 0), pdcch(2, 1, 1, 0, dci_type=grid.C_RNTI_F1_0, ss_id=1),
                       pdcch(3, 1, 2, 0, dci_type=grid.TC_RNTI_F1_0, ss_id=2)])
        # DCI 1_1, DCI 1_0 in a common search space, DCI 1_0 in a UE-specific one, and no PDCCH in the slot.
        self.assertEqual(starts, {("ue_pdschs", 0): 2, ("ue_pdschs", 1): 8, ("ue_pdschs", 2): 2, ("ue_pdschs", 3): 2})

    def test_common_pdschs(self):
        starts = self.starts(
            sib_pdschs=[common_pdsch(vrbs(0, 4), is_sib1=True), common_pdsch(vrbs(4, 4), is_sib1=False)],
            rar_pdschs=[common_pdsch(vrbs(0, 4), rnti=7, grants=[])],
            paging_pdschs=[common_pdsch(vrbs(0, 4), paging_ues=[])],
            dl_pdcchs=[pdcch(0xFFFF, 0, 0, 2, dci_type=grid.SI_F1_0, ss_id=0),
                       pdcch(0xFFFF, 1, 0, 2, dci_type=grid.SI_F1_0, ss_id=1),
                       pdcch(7, 1, 4, 2, dci_type=grid.RA_F1_0, ss_id=1)])
        # SIBs by the order of their PDCCHs, the RAR by its RNTI, and paging, without a PDCCH, from CORESET 0.
        self.assertEqual(starts, {("sib_pdschs", 0): 10, ("sib_pdschs", 1): 12, ("rar_pdschs", 0): 8,
                                  ("paging_pdschs", 0): 10})


class PdcchTest(unittest.TestCase):
    """CRBs of the CCEs of PDCCHs, TS 38.211 7.3.2.2."""

    def test_not_interleaved(self):
        # 2 symbols: a CCE is 6 REGs, 3 RBs. The CRBs of the CORESET are 8 to 19 and 32 to 43.
        coreset = {"id": 1, "crbs": [{"start": 8, "length": 12}, {"start": 32, "length": 12}], "duration": 2}
        self.assertEqual(grid.pdcch_crbs(coreset, 0, 2), [(8, 6)])
        self.assertEqual(grid.pdcch_crbs(coreset, 3, 2), [(17, 3), (32, 3)])

    def test_interleaved(self):
        # 24 RBs, 2 symbols: 48 REGs in 8 bundles of 6, interleaver size 2, so 4 columns, shift 1. CCEs 0 to 3 are
        # bundles f(0..3) = 1, 5, 2, 6, of 3 RBs each.
        coreset = {"id": 0, "crbs": [{"start": 3, "length": 24}], "duration": 2,
                   "interleaving": {"reg_bundle_size": 6, "interleaver_size": 2, "shift_index": 1}}
        self.assertEqual(grid.pdcch_crbs(coreset, 0, 4), [(6, 6), (18, 6)])

    def test_allocations(self):
        cell = grid.cell_of(next(schedtrace.read(DATA / "schedtrace_cell0.bin")))
        allocs = grid.allocations(slot(ul_pdcchs=[pdcch(5, 1, 0, 1, ss_id=1)]), cell)
        # Search space 1 monitors symbol 0. CORESET 1, 1 symbol, 24 RBs, has 4 bundles of 6 RBs, interleaver size 2, so
        # 2 columns, shift 1: CCEs 0 and 1 are bundles 1 and 3, RBs 6 to 11 and 18 to 23 of CRBs 8 to 19 and 32 to 43.
        self.assertEqual([(a["channel"], a["sym_start"], a["sym_length"], a["crb_start"], a["crb_length"], a["item"])
                          for a in allocs], [("PDCCH", 0, 1, 14, 6, ["ul_pdcchs", 0]), ("PDCCH", 0, 1, 38, 6, ["ul_pdcchs", 0])])

    def test_allocations_of_pdcchs_name_their_items(self):
        cell = grid.cell_of(next(schedtrace.read(DATA / "schedtrace_cell0.bin")))
        allocs = grid.allocations(slot(dl_pdcchs=[pdcch(5, 1, 0, 1, ss_id=1), pdcch(6, 1, 2, 1, ss_id=1)]), cell)
        self.assertEqual(sorted({tuple(a["item"]) for a in allocs}), [("dl_pdcchs", 0), ("dl_pdcchs", 1)])



class PrachTest(unittest.TestCase):
    """PRACH occasions: msg1-FrequencyStart 3 in the UL BWP from CRB 0, PRACH and PUSCH subcarrier spacing 30 kHz."""

    def setUp(self):
        self.cell = grid.cell_of(next(schedtrace.read(DATA / "schedtrace_cell0.bin")))

    def rects(self, **prach):
        prach = {"pci": 1, "format": 0, "nof_occasions": 1, "index_fd_ra": 0, "start_symbol": 0, "nof_cs": 0,
                 "nof_fd_ra": 1, "start_preamble_index": 0, "nof_preamble_indexes": 64, **prach}
        allocs = grid.allocations(slot(prachs=[prach]), self.cell)
        return [(a["format"], a["occasion"], a["fd_ra"], a["sym_start"], a["sym_length"], a["crb_start"], a["crb_length"])
                for a in allocs]

    def test_short_format(self):
        # Format A1: 2 symbols per occasion, 12 RBs per frequency occasion. Frequency occasion 1 of the 2 of msg1-FDM,
        # which has a Prach of its own.
        self.assertEqual(self.rects(format=4, nof_occasions=2, start_symbol=2, index_fd_ra=1, nof_fd_ra=2), [
            ("A1", 0, 1, 2, 2, 15, 12), ("A1", 1, 1, 4, 2, 15, 12),
        ])

    def test_long_format_lasts_past_its_slot(self):
        # Format 0: 1.25 kHz, 3 RBs at 30 kHz, 1 ms, 2 slots of 14 symbols at 30 kHz.
        (rect,) = self.rects(format=0)
        self.assertEqual(rect, ("0", 0, 0, 0, 28, 3, 3))
        rects = grid.allocations(slot(prachs=[{"pci": 1, "format": 0, "nof_occasions": 1, "index_fd_ra": 0,
                                               "start_symbol": 0, "nof_cs": 0, "nof_fd_ra": 1,
                                               "start_preamble_index": 0, "nof_preamble_indexes": 64}]), self.cell)
        # Occupancy counts only its slot: the 3 CRBs of group 0 of 4 CRBs, all 14 symbols.
        self.assertEqual(grid.occupancy(rects, 4), {(0, "PRACH"): 0.25, (1, "PRACH"): 0.5})
