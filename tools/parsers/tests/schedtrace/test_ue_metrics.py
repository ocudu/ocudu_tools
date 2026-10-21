# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import unittest
from pathlib import Path

from parsers import schedtrace
from parsers.schedtrace import grid, ue_metrics

DATA = Path(__file__).parent / "data"


def pdsch(rnti, mcs, tbs, new_data=True, nof_retxs=0, rbs=None):
    return {"rnti": rnti, "ue_index": rnti - 0x4600, "harq_id": 0, "nof_retxs": nof_retxs, "k1": 4,
            "rbs": rbs or {"type": "VrbAlloc", "vrb_start": 0, "vrb_length": 10},
            "codewords": [{"mcs_index": mcs, "tb_size_bytes": tbs, "new_data": new_data, "rv_index": 0}]}


def pusch(rnti, tbs, nof_retxs=0, layers=1):
    return {"rnti": rnti, "ue_index": rnti - 0x4600, "harq_id": 1, "tb_size_bytes": tbs, "nof_layers": layers,
            "rv_index": 0, "nof_retxs": nof_retxs, "k2": 4, "rbs": {"type": "VrbAlloc", "vrb_start": 0, "vrb_length": 5}}


def ack(rnti, status, tbs=1000):
    return {"type": "HarqAckEvent", "slot_rx": 0, "ue_idx": rnti - 0x4600, "rnti": rnti, "harq_id": 0, "status": status,
            "tbs": tbs}


def slot(t_us, inputs=(), latency=10, **decision):
    return {"type": "CellSlotEvent", "slot_tx": 0, "timestamp_us": t_us, "latency_us": latency, "inputs": list(inputs),
            "decision": decision}


class UeMetricsTest(unittest.TestCase):
    def setUp(self):
        # DL BWP of 52 PRBs from CRB 2, RBGs of 4.
        self.cell = grid.cell_of(next(schedtrace.read(DATA / "schedtrace_cell0.bin")))

    def test_slot_items(self):
        rbg = {"type": "RbgAlloc", "nof_rbgs": 14, "bitmap": 0b11 << 12}
        items = ue_metrics.slot_items(slot(0, [ack(0x4601, "Ack"), {"type": "SrEvent", "slot_rx": 0, "ue_idx": 1,
                                                                        "rnti": 0x4601}],
                                           ue_pdschs=[pdsch(0x4601, 27, 5000, rbs=rbg)], puschs=[pusch(0x4601, 900)]),
                                      self.cell)
        # RBGs 0 and 1: CRBs 2 and 3, then 4 to 7, 6 PRBs.
        self.assertEqual(items["dl_grants"], [("0x4601", 1, 0, 27, 5000, 6, True, 0, 4, None)])
        self.assertEqual(items["ul_grants"], [("0x4601", 1, 1, 900, 5, 1, 0, 0, 4)])
        self.assertEqual(items["harq_acks"], [("0x4601", 1, 0, "Ack", 1000)])
        self.assertEqual(items["srs"], [("0x4601", 1)])

    def test_aggregates(self):
        agg = ue_metrics.Aggregator()
        events = [
            # Bin 0: a new transmission and a retransmission, and an ACK, a NACK and a DTX.
            slot(1000, [ack(0x4601, "Ack", 2500), ack(0x4601, "Nack")], ue_pdschs=[pdsch(0x4601, 20, 4000)],
                 puschs=[pusch(0x4601, 1000, layers=2)]),
            slot(2000, [ack(0x4601, "Dtx")], latency=30, ue_pdschs=[pdsch(0x4601, 10, 4000, False, 1)],
                 puschs=[pusch(0x4601, 1000, nof_retxs=1)]),
            # Bin 1.
            slot(11000, [{"type": "CsiReportEvent", "slot_rx": 0, "ue_idx": 1, "rnti": 0x4601, "first_tb_wb_cqi": 12,
                          "ri": 2, "rsrp_dbm": [-80]}]),
        ]
        for record, e in enumerate(events, 1):
            agg.add(record, e, ue_metrics.slot_items(e, self.cell))
        rows = [dict(zip(("t", "record", *ue_metrics.UE_FIELDS), r)) for r in agg.ue_rows()]
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual((first["t"], first["record"], first["rnti"], first["ue_index"]), (0, 1, "0x4601", 1))
        # ACKed bytes and new transmission bytes, over 10 ms.
        self.assertEqual((first["dl_brate"], first["dl_sched_brate"]), (2500 * 8 / 0.01, 4000 * 8 / 0.01))
        self.assertEqual((first["dl_mcs"], first["dl_nof_prbs"], first["dl_nof_newtx"], first["dl_nof_retx"]),
                         (15, 20, 1, 1))
        # NACKs and DTXs are not ok, as in METRICS.
        self.assertEqual((first["dl_nof_ok"], first["dl_nof_nok"], first["dl_nof_dtx"]), (1, 2, 1))
        self.assertAlmostEqual(first["dl_error_rate"], 200 / 3)
        self.assertEqual((first["ul_sched_brate"], first["ul_nof_newtx"], first["ul_nof_retx"], first["ul_layers"]),
                         (1000 * 8 / 0.01, 1, 1, 1.5))
        self.assertEqual((rows[1]["cqi"], rows[1]["dl_ri"], rows[1]["rsrp"], rows[1]["dl_mcs"]), (12, 2, -80, None))
        cell = [dict(zip(("t", "record", *ue_metrics.CELL_FIELDS), r)) for r in agg.cell_rows()]
        self.assertEqual((cell[0]["latency_avg"], cell[0]["latency_max"], cell[0]["nof_slots"], cell[0]["dl_nof_ues"]),
                         (20, 30, 2, 1))

    def test_cqi_and_ri_are_means_of_a_bin(self):
        agg = ue_metrics.Aggregator()
        csi = [{"type": "CsiReportEvent", "slot_rx": 0, "ue_idx": 1, "rnti": 0x4601, "first_tb_wb_cqi": cqi, "ri": ri}
               for cqi, ri in ((12, 1), (15, 2))]
        e = slot(1000, csi)
        agg.add(1, e, ue_metrics.slot_items(e, self.cell))
        values = dict(zip(ue_metrics.UE_FIELDS, agg.ue_rows()[0][2:]))
        # Unrounded, so that means over bins are not biased.
        self.assertEqual((values["cqi"], values["dl_ri"]), (13.5, 1.5))

    def test_dl_mcs_is_the_mean_of_every_codeword(self):
        two_cws = pdsch(0x4601, 20, 1000)
        two_cws["codewords"].append({"mcs_index": 23, "tb_size_bytes": 1500, "new_data": True, "rv_index": 0})
        e = slot(1000, ue_pdschs=[two_cws, pdsch(0x4601, 22, 1000)])
        items = ue_metrics.slot_items(e, self.cell)
        self.assertEqual([(g[3], g[4], g[9]) for g in items["dl_grants"]], [(20, 2500, 23), (22, 1000, None)])
        agg = ue_metrics.Aggregator()
        agg.add(1, e, items)
        values = dict(zip(ue_metrics.UE_FIELDS, agg.ue_rows()[0][2:]))
        # (20 + 23 + 22) / 3, and the TBS of both codewords.
        self.assertAlmostEqual(values["dl_mcs"], 65 / 3)
        self.assertEqual(values["dl_sched_brate"], 3500 * 8 / 0.01)

    def test_round_half_up_rounds_as_std_round(self):
        # Halves round up, as std::round of METRICS, not to the even of round().
        self.assertEqual([ue_metrics.round_half_up(v) for v in (20.5, 13.5, 21.49, 0.0, None)], [21, 14, 21, 0, None])

    def test_bin_size_and_totals(self):
        # 1 s bins: one bin with all 60 slots, each with a 1000 byte new transmission and its ACK.
        events = [slot(t, [ack(0x4601, "Ack")], ue_pdschs=[pdsch(0x4601, 20, 1000)]) for t in range(0, 30000, 500)]
        agg = ue_metrics.Aggregator(bin_s=1)
        for record, e in enumerate(events, 1):
            agg.add(record, e, ue_metrics.slot_items(e, self.cell))
        (row,) = agg.ue_rows()
        values = dict(zip(ue_metrics.UE_FIELDS, row[2:]))
        self.assertEqual((row[0], values["dl_nof_newtx"], values["dl_brate"]), (0, 60, 60 * 1000 * 8))
        # Over the 30 ms of the slots, as for a whole trace.
        (total,) = agg.ue_totals(0.03)
        values = dict(zip(ue_metrics.UE_FIELDS, total[2:]))
        self.assertEqual((total[0], total[1], values["rnti"], values["dl_mcs"]), (0, 0, "0x4601", 20))
        self.assertAlmostEqual(values["dl_brate"], 60 * 1000 * 8 / 0.03)

    def test_slots_at_a_bin_edge_start_its_bin(self):
        agg = ue_metrics.Aggregator(bin_s=0.01)
        e = slot(1_760_000_000_000_000, ue_pdschs=[pdsch(0x4601, 20, 1000)])
        agg.add(1, e, ue_metrics.slot_items(e, self.cell))
        self.assertAlmostEqual(agg.ue_rows()[0][0], 1_760_000_000.0)
        self.assertEqual(list(agg.cells), [176_000_000_000])

    def test_merge_of_other_bins_raises(self):
        with self.assertRaises(ValueError):
            ue_metrics.Aggregator(bin_s=1).merge(ue_metrics.Aggregator(bin_s=0.5))

    def test_bin_size_is_whole_microseconds(self):
        agg = ue_metrics.Aggregator(bin_s=1.5e-6)
        e = slot(300, ue_pdschs=[pdsch(0x4601, 20, 1000)])
        agg.add(1, e, ue_metrics.slot_items(e, self.cell))
        # Bins of 2 us: the slot at 300 us starts bin 150.
        self.assertEqual((agg.bin_s, agg.ue_rows()[0][0]), (2e-6, 300e-6))

    def test_unknown_harq_status_is_counted_as_none(self):
        e = slot(0, [ack(0x4601, 7), ack(0x4601, 2)])
        items = ue_metrics.slot_items(e, self.cell)
        self.assertEqual([a[3] for a in items["harq_acks"]], ["Status7", "Dtx"])
        agg = ue_metrics.Aggregator()
        agg.add(1, e, items)
        values = dict(zip(ue_metrics.UE_FIELDS, agg.ue_rows()[0][2:]))
        self.assertEqual((values["dl_nof_ok"], values["dl_nof_nok"], values["dl_nof_dtx"]), (0, 1, 1))

    def test_msg3_has_no_ue_index(self):
        msg3 = dict(pusch(0x4601, 11), ue_index=ue_metrics.MAX_NOF_DU_UES)
        e = slot(0, puschs=[msg3])
        items = ue_metrics.slot_items(e, self.cell)
        self.assertEqual(items["ul_grants"][0][:2], ("0x4601", None))
        agg = ue_metrics.Aggregator()
        agg.add(1, e, items)
        self.assertIsNone(dict(zip(ue_metrics.UE_FIELDS, agg.ue_rows()[0][2:]))["ue_index"])
        # Later grants of the UE, in its bin and in later bins, give its index.
        later = [slot(1000, puschs=[pusch(0x4601, 900)]), slot(20000, puschs=[msg3])]
        for record, e in enumerate(later, 2):
            agg.add(record, e, ue_metrics.slot_items(e, self.cell))
        self.assertEqual([r[3] for r in agg.ue_rows()], [1, None])
        self.assertEqual(agg.ue_totals(0.03)[0][3], 1)
        other = ue_metrics.Aggregator()
        other.add(9, slot(1500, puschs=[msg3]), ue_metrics.slot_items(slot(1500, puschs=[msg3]), self.cell))
        agg.merge(other)
        self.assertEqual(agg.ue_rows()[0][3], 1)

    def test_merge_equals_one_pass(self):
        events = [slot(t, [ack(0x4601, "Ack")], ue_pdschs=[pdsch(0x4601, 20, 1000)]) for t in range(0, 30000, 500)]
        whole, first, second = ue_metrics.Aggregator(), ue_metrics.Aggregator(), ue_metrics.Aggregator()
        for record, e in enumerate(events, 1):
            items = ue_metrics.slot_items(e, self.cell)
            whole.add(record, e, items)
            (first if record <= 25 else second).add(record, e, items)
        first.merge(second)
        self.assertEqual(first.ue_rows(), whole.ue_rows())
        self.assertEqual(first.cell_rows(), whole.cell_rows())


if __name__ == "__main__":
    unittest.main()
