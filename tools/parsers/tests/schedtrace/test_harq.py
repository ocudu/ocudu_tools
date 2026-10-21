# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import unittest
from pathlib import Path

from parsers import schedtrace
from parsers.schedtrace import harq

DATA = Path(__file__).parent / "data"
# Slots per frame of the events, and the slot after which slot_tx wraps.
SPF = 20
WRAP = harq.NOF_SFNS * SPF
RNTI = 0x4601


def pdsch(harq_id=0, nof_retxs=0, k1=4, tbs=1000, rnti=RNTI):
    return {"rnti": rnti, "ue_index": 1, "harq_id": harq_id, "nof_retxs": nof_retxs, "k1": k1, "sym_start": 2,
            "sym_length": 12, "rbs": {"type": "VrbAlloc", "vrb_start": 0, "vrb_length": 10},
            "codewords": [{"mcs_index": 20, "tb_size_bytes": tbs, "new_data": nof_retxs == 0, "rv_index": 0}]}


def pdcch(harq_id=0, ndi=True, rnti=RNTI, dci_type=5):
    return {"rnti": rnti, "dci_type": dci_type, "coreset_id": 1, "ss_id": 2, "cce": 0, "aggr_lvl": 2,
            "harq_id": harq_id, "ndi": ndi, "rv": 0, "mcs": 20, "pucch_res_ind": 0}


def pusch(harq_id=1, nof_retxs=0, k2=4, tbs=500, rnti=RNTI):
    return {"rnti": rnti, "ue_index": 1, "harq_id": harq_id, "tb_size_bytes": tbs, "nof_layers": 1, "rv_index": 0,
            "nof_retxs": nof_retxs, "k2": k2, "sym_start": 0, "sym_length": 14,
            "rbs": {"type": "VrbAlloc", "vrb_start": 0, "vrb_length": 5}}


def pucch(rnti=RNTI, harq_bits=1):
    return {"rnti": rnti, "res_idx": 0, "nof_bits_harq_ack": harq_bits, "nof_bits_sr": 0, "nof_bits_csi1": 0}


def ack(slot_rx, status="Ack", harq_id=0, rnti=RNTI):
    return {"type": "HarqAckEvent", "slot_rx": slot_rx % WRAP, "ue_idx": 1, "rnti": rnti, "harq_id": harq_id,
            "status": status, "tbs": 1000}


def slot(slot_tx, inputs=(), **decision):
    return {"type": "CellSlotEvent", "slot_tx": slot_tx % WRAP, "timestamp_us": slot_tx * 500, "latency_us": 10,
            "inputs": list(inputs), "decision": decision}


def run(events):
    return list(harq.transactions(((e["slot_tx"], e) for e in events), SPF))


class HarqTest(unittest.TestCase):
    def test_retx_after_nack(self):
        trs = run([
            slot(100, dl_pdcchs=[pdcch()], ue_pdschs=[pdsch()]),
            slot(104, pucchs=[pucch()]),
            slot(108, inputs=[ack(104, "Nack")], dl_pdcchs=[pdcch()], ue_pdschs=[pdsch(nof_retxs=1)]),
            slot(112, puschs=[pusch()]),
            slot(115, inputs=[ack(112)]),
        ])
        dl = [t for t in trs if t.dir == "DL"]
        self.assertEqual(len(dl), 1)
        tr = dl[0]
        self.assertEqual(tr.rnti, "0x4601")
        self.assertEqual((tr.harq_id, tr.tbs, tr.nof_tx, tr.ended_by), (0, 1000, 2, "ack"))
        self.assertEqual(tr.outcome(), "ACK at tx 2")
        self.assertEqual([(r.kind, r.slot_tx, r.tx) for r in tr.rows], [
            (harq.PDCCH, 100, 1), (harq.PDSCH, 100, 1), (harq.PUCCH, 104, 1), (harq.HARQ_ACK, 108, 1),
            (harq.PDCCH, 108, 2), (harq.PDSCH, 108, 2), (harq.UCI_ON_PUSCH, 112, 2), (harq.HARQ_ACK, 115, 2)])
        self.assertTrue(tr.rows[6].params["inferred"])
        self.assertEqual(tr.rows[3].params["slot_rx"], "5.4")
        self.assertEqual(tr.rows[0].params["al"], 4)

    def test_new_tx_ends_transaction_without_ack(self):
        trs = run([
            slot(100, ue_pdschs=[pdsch()]),
            slot(108, inputs=[ack(104, "Dtx")]),
            slot(110, ue_pdschs=[pdsch(nof_retxs=1)]),
            slot(118, inputs=[ack(114, "Nack")]),
            slot(130, ue_pdschs=[pdsch(tbs=2000)]),
        ])
        first, second = trs
        self.assertEqual((first.ended_by, first.nof_tx, first.statuses), ("new_tx", 2, ["Dtx", "Nack"]))
        self.assertEqual(first.outcome(), "failed after 2 NACK/DTX")
        self.assertEqual((second.ended_by, second.tbs), ("end", 2000))
        self.assertEqual(second.outcome(), "no HARQ-ACK report by the end of the events")

    def test_ack_and_reuse_in_one_slot(self):
        # The report of a slot is received before its decision reuses the process.
        trs = run([
            slot(100, ue_pdschs=[pdsch()]),
            slot(110, inputs=[ack(104)], ue_pdschs=[pdsch(tbs=3000)]),
        ])
        self.assertEqual([(t.ended_by, t.tbs) for t in trs], [("ack", 1000), ("end", 3000)])

    def test_processes_and_ues_apart(self):
        trs = run([
            slot(100, ue_pdschs=[pdsch(harq_id=0), pdsch(harq_id=0, rnti=RNTI + 1)]),
            slot(101, ue_pdschs=[pdsch(harq_id=1)]),
            slot(105, inputs=[ack(104, harq_id=0, rnti=RNTI + 1), ack(105, harq_id=1)]),
        ])
        self.assertEqual(sorted((t.rnti, t.harq_id, t.ended_by) for t in trs),
                         [("0x4601", 0, "end"), ("0x4601", 1, "ack"), ("0x4602", 0, "ack")])

    def test_report_of_another_slot_is_not_matched(self):
        trs = run([slot(100, ue_pdschs=[pdsch()]), slot(110, inputs=[ack(105)])])
        self.assertEqual(trs[0].statuses, [])

    def test_retx_without_new_tx_in_events(self):
        tr, = run([slot(100, ue_pdschs=[pdsch(nof_retxs=2)])])
        self.assertFalse(tr.started)
        self.assertEqual(tr.nof_tx, 1)

    def test_ul(self):
        trs = run([
            slot(100, ul_pdcchs=[pdcch(harq_id=1, dci_type=2)]),
            slot(104, puschs=[pusch()]),
            # A retransmission without PDCCH, non-adaptive.
            slot(114, puschs=[pusch(nof_retxs=1)]),
            slot(120, ul_pdcchs=[pdcch(harq_id=1, ndi=False, dci_type=2)]),
            slot(126, puschs=[pusch(k2=6)]),
        ])
        first, second = trs
        self.assertEqual([(r.kind, r.slot_tx, r.tx) for r in first.rows],
                         [(harq.PDCCH, 100, 1), (harq.PUSCH, 104, 1), (harq.PUSCH, 114, 2)])
        self.assertEqual(first.rows[0].params["dci"], "c-rnti 0_1")
        self.assertEqual((first.ended_by, first.outcome()), ("new_tx", "outcome unknown, the trace has no UL CRC"))
        self.assertEqual([(r.kind, r.slot_tx) for r in second.rows], [(harq.PDCCH, 120), (harq.PUSCH, 126)])

    def test_sfn_wrap(self):
        trs = run([
            slot(WRAP - 2, ue_pdschs=[pdsch()]),
            slot(WRAP + 2, pucchs=[pucch()]),
            slot(WRAP + 5, inputs=[ack(WRAP + 2)]),
        ])
        self.assertEqual([r.kind for r in trs[0].rows], [harq.PDSCH, harq.PUCCH, harq.HARQ_ACK])
        self.assertEqual(trs[0].rows[1].slot_tx, 2)

    def test_find(self):
        events = [
            slot(100, dl_pdcchs=[pdcch()], ue_pdschs=[pdsch()], puschs=[pusch()]),
            slot(108, inputs=[ack(104)]),
            slot(110, ue_pdschs=[pdsch(tbs=7)]),
        ]
        keyed = [(e["slot_tx"], e) for e in events]
        self.assertEqual(harq.find(keyed, SPF, 100, ["dl_pdcchs", 0]).tbs, 1000)
        self.assertEqual(harq.find(keyed, SPF, 110, ("ue_pdschs", 0)).tbs, 7)
        self.assertEqual(harq.find(keyed, SPF, 100, ("puschs", 0)).dir, "UL")
        self.assertIsNone(harq.find(keyed, SPF, 108, ("ue_pdschs", 0)))

    def test_sample(self):
        events = list(enumerate(schedtrace.read(DATA / "schedtrace_cell0.bin", grid_only=True), 1))
        trs = list(harq.transactions(events, SPF))
        for tr in trs:
            self.assertLessEqual(sum(s == "Ack" for s in tr.statuses), 1)
            self.assertEqual(tr.rows, sorted(tr.rows, key=lambda r: r.event))


if __name__ == "__main__":
    unittest.main()
