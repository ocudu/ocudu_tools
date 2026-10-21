#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for ocudu_metrics.py, which imports the parsers package of this repo through the symlink in
skills/analyze-ran-log/scripts/_lib, so it also checks that the symlink resolves.

The fixture holds real gnb.log METRICS lines between other log lines.

Usage:
    python3 test_ocudu_metrics.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "ocudu_metrics.py"

SAMPLE_GNB_LOG = """\
2026-06-29T14:10:06.000000 [GNB     ] [I] Built in RelWithDebInfo mode using commit 404c93d34d on branch main
2026-06-29T14:10:06.324561 [METRICS ] MAC cell pci=1 metrics: slots=[853.19, 864.0) nof_slots=201 slot_duration=500usec nof_voluntary_context_switches=0 nof_involuntary_context_switches=0 wall_clock_latency=[avg=7usec max=88usec max_slot=853.19] sched_latency=[avg=4usec max=61usec max_slot=853.19] dl_tti_req_latency=[avg=3usec max=79usec max_slot=856.2] tx_data_req_latency=[avg=0usec max=0usec max_slot=0.0] ul_tti_req_latency=[avg=2usec max=25usec max_slot=853.19] slot_ind_dequeue_latency=[avg=15usec max=42usec max_slot=856.14] slot_ind_msg_time_diff=[avg=500usec max=505usec max_slot=855.9]
2026-06-29T14:10:11.345400 [METRICS ] Scheduler UE ue=34 pci=1 rnti=0x462d metrics: cqi=n/a dl_ri=n/a dl_mcs=0 dl_brate=0bps dl_nof_ok=0 dl_nof_nok=0 dl_error_rate=0% dl_bs=303 dl_nof_prbs=0 pusch_snr_db=n/a pusch_rsrp_db=n/a ul_ri=1 ul_mcs=0 ul_brate=0bps ul_nof_ok=0 ul_nof_nok=0 ul_error_rate=0% ul_nof_prbs=0 bsr=0 sr_count=0 f0f1_invalid_harqs=0 f2f3f4_invalid_harqs=0 f2f3f4_invalid_csis=15 pusch_invalid_harqs=0 pusch_invalid_csis=0 ta= 0s srs_ta=n/a last_phr=n/a max_pdsch_distance=0ms max_pusch_distance=0ms avg_ul_ce_delay=n/a max_ul_ce_delay=n/a avg_crc_delay=n/a max_crc_delay=n/a avg_pusch_harq_delay=n/a max_pusch_harq_delay=n/a avg_pucch_harq_delay=n/a max_pucch_harq_delay=n/a avg_sr_to_pusch_delay=n/a max_sr_to_pusch_delay=n/a
2026-06-29T14:10:11.345500 [SCHED   ] [I] [   35.0] Slot decisions pci=1 t=5us (1 PDSCH, 0 PUSCHs, 0 PUCCHs)
2026-06-29T14:10:11.345650 [METRICS ] Scheduler UE ue=81 pci=1 rnti=0x4666 metrics: cqi=15 dl_ri=1.0 dl_mcs=13 dl_brate=8.78kbps dl_nof_ok=8 dl_nof_nok=0 dl_error_rate=0% dl_bs=0 dl_nof_prbs=72 dl_olla=0 pusch_snr_db=49.3 pusch_rsrp_db=-11.0 ul_ri=1 ul_mcs=22 ul_brate=12.9kbps ul_nof_ok=3 ul_nof_nok=0 ul_error_rate=0% ul_nof_prbs=36 bsr=0 sr_count=3 f0f1_invalid_harqs=0 f2f3f4_invalid_harqs=0 f2f3f4_invalid_csis=3 pusch_invalid_harqs=0 pusch_invalid_csis=0 ul_olla=0 ta=-8ns srs_ta=n/a last_phr=38 max_pdsch_distance=80ms max_pusch_distance=40ms avg_ul_ce_delay=2.33ms max_ul_ce_delay=2.5ms avg_crc_delay=2.17ms max_crc_delay=2.5ms avg_pusch_harq_delay=n/a max_pusch_harq_delay=n/a avg_pucch_harq_delay=2ms max_pucch_harq_delay=2ms avg_sr_to_pusch_delay=5ms max_sr_to_pusch_delay=5ms max_dl_lcid0_flush_delay=4.5ms max_dl_lcid1_flush_delay=1.5ms
"""


class OcuduMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "gnb.log"
        self.log.write_text(SAMPLE_GNB_LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args):
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.log), *args], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_lists_layers(self):
        out = self.run_script()
        self.assertIn("mac: 1 rows", out)
        self.assertIn("sched_ue: 2 rows", out)
        self.assertIn("dl_brate [bps]", out)

    def test_lists_unparsed_kinds(self):
        self.assertNotIn("not parsed", self.run_script())
        ngap = "2026-06-29T14:10:12.000000 [METRICS ] CU-CP NGAP metrics: [ amf_name=amf connected=true ]\n"
        rrc = "2026-06-29T14:10:12.000001 [METRICS ] CU-CP RRC metrics: [ gnb_du_id=0 mean_nof_rrc_connections=0 ]\n"
        worker = "2026-06-29T14:10:12.000002 [METRICS ] [I] Stopped the executor metrics backend worker\n"
        self.log.write_text(SAMPLE_GNB_LOG + ngap + ngap + rrc + worker)
        self.assertIn("3 METRICS lines not parsed: CU-CP NGAP metrics (2), CU-CP RRC metrics (1)", self.run_script())

    def test_stats_by_context_field(self):
        out = self.run_script("--layer", "sched_ue", "--fields", "dl_brate,cqi", "--by", "ue")
        self.assertIn("ue=81: 1 rows", out)
        self.assertRegex(out, r"dl_brate +n=1 +min=8780 mean=8780 max=8780 bps")
        # cqi is n/a for UE 34.
        self.assertRegex(out, r"(?s)ue=34: 1 rows.*cqi +\(no numeric values\)")

    def test_rows_with_filter(self):
        out = self.run_script("--layer", "sched_ue", "--where", "rnti=0x4666", "--rows", "--fields", "ue,dl_mcs")
        self.assertIn("Layer sched_ue: 1 rows", out)
        self.assertRegex(out, r"\n  5  14:10:11.345650  81  13\n")

    def test_time_bounds(self):
        # A date and time joined by a space, and a date, are ISO bounds, not times of day.
        for args, nof_rows in ((("--after", "2026-06-29 14:10:11.3456"), 1), (("--before", "2026-06-29 14:10:10"), 0),
                               (("--after", "14:10:11.3456"), 1), (("--before", "2026-06-29"), 2),
                               (("--after", "2026-06-30"), 0)):
            out = self.run_script("--layer", "sched_ue", "--rows", *args)
            self.assertIn(f"Layer sched_ue: {nof_rows} rows", out, args)

    def test_parallel_workers(self):
        self.assertEqual(self.run_script("-j", "2", "--layer", "mac"), self.run_script("--layer", "mac"))

    def test_unknown_layer(self):
        cmd = [sys.executable, str(SCRIPT), str(self.log), "--layer", "nope"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("unknown layer", res.stderr)

    def test_invalid_rnti(self):
        cmd = [sys.executable, str(SCRIPT), str(self.log), "--layer", "sched_ue", "--where", "rnti=zz"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("invalid RNTI", res.stderr)


if __name__ == "__main__":
    unittest.main()
