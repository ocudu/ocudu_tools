#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for ocudu_log_search.py against literal OCUDU gnb.log examples.

The fixture below is assembled from real gnb.log lines (boot + CONFIG echo,
NG Setup, a UE's RRC setup with the CU-CP-F1/DU-F1 split, a multi-line SCHED
slot-decision block, and a NGAP release) so the block-splitting, CONFIG-echo
skip, and layer/level/ue/rnti/pci/timestamp filters are all exercised against
genuine log formatting rather than hand-written approximations.

Usage:
    python3 test_ocudu_log_search.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "ocudu_log_search.py"

SAMPLE_GNB_LOG = """\
2026-07-27T15:44:57.314934 [GNB     ] [I] Built in RelWithDebInfo mode using commit 404c93d34d on branch sched_temp_ue_repo
2026-07-27T15:44:57.318976 [CONFIG  ] [D] gNB input configuration (all values):
gnb_id: 411
gnb_id_bit_length: 32
ran_node_name: ocudu01
2026-07-27T15:44:58.108838 [GNB     ] [W] CPU0 scaling governor is not set to performance, which may hinder performance. You can set it to performance using the "ocudu_performance" script
2026-07-27T15:44:58.108853 [GNB     ] [W] CPU1 scaling governor is not set to performance, which may hinder performance. You can set it to performance using the "ocudu_performance" script
2026-07-27T15:44:58.453746 [NGAP    ] [I] "NG Setup Procedure" started...
2026-07-27T15:44:58.453749 [NGAP    ] [I] Tx PDU: NGSetupRequest
2026-07-27T15:44:58.453978 [NGAP    ] [I] Rx PDU: NGSetupResponse
2026-07-27T15:44:58.454004 [NGAP    ] [I] "NG Setup Procedure" finished successfully
2026-07-27T15:45:03.548705 [RRC     ] [I] ue=0 c-rnti=0x4602: CCCH UL rrcSetupRequest
2026-07-27T15:45:03.548710 [RRC     ] [I] ue=0 c-rnti=0x4602: "RRC Setup Procedure" started...
2026-07-27T15:45:03.548768 [PDCP    ] [I] ue=0 SRB1 DL: PDCP configured. rb_type=SRB rlc_mode=AM sn_size=12 discard_timer=infinity count_notify=3221225472 count_max=4294967294 warn_on_drop=false test_mode=false
2026-07-27T15:45:03.548812 [PDCP    ] [I] ue=0 SRB1 UL: PDCP configured. rb_type=SRB rlc_mode=AM sn_size=12 t_reordering=infinity count_notify=3221225472 count_max=4294967294 warn_on_drop=false
2026-07-27T15:45:03.548840 [RRC     ] [I] ue=0 c-rnti=0x4602: CCCH DL rrcSetup
2026-07-27T15:45:03.548847 [CU-CP-F1] [I] Tx PDU du=0 ue=0 cu_ue=0 du_ue=0: DLRRCMessageTransfer
2026-07-27T15:45:03.548859 [DU-F1   ] [I] Rx PDU du=0 ue=0 cu_ue=0 du_ue=0: DLRRCMessageTransfer
2026-07-27T15:45:03.550462 [SCHED   ] [D] [    91.0] Processed slot events pci=1:
- RLC Buffer State: ue=0 lcid=0 pending_bytes=302
2026-07-27T15:45:03.588689 [SCHED   ] [W] [    92.5] rnti=0x4602: Discarding ACK info. Cause: DL HARQ for uci slot=91.17 and HARQ-ACK bit=0 not found.
2026-07-27T15:45:07.259291 [NGAP    ] [I] ue=0: Ignoring UEContextReleaseRequest. Cause: UE has no NGAP context
"""


class OcuduLogSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        (Path(cls.tmpdir.name) / "gnb.log").write_text(SAMPLE_GNB_LOG)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def run_search(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), self.tmpdir.name, *args],
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def run_count(self, *args: str) -> int:
        return int(self.run_search(*args, "--count").strip())

    def test_config_echo_skipped_by_default(self):
        # Regression check: the skip used to key off the substring "Input
        # configuration" (capital I), which stopped matching once OCUDU
        # started logging "gNB input configuration" (lowercase i).
        self.assertEqual(self.run_count(), 17)
        self.assertEqual(self.run_count("--include-config-echo"), 18)

    def test_layer_filter(self):
        self.assertEqual(self.run_count("--layer", "NGAP"), 5)
        self.assertEqual(self.run_count("--layer", "ngap"), 5)  # case-insensitive

    def test_level_filter(self):
        self.assertEqual(self.run_count("--level", "W"), 3)

    def test_ue_filter(self):
        self.assertEqual(self.run_count("--ue", "0"), 9)

    def test_rnti_filter(self):
        self.assertEqual(self.run_count("--rnti", "4602"), 4)

    def test_pci_filter(self):
        self.assertEqual(self.run_count("--pci", "1"), 1)

    def test_timestamp_window(self):
        self.assertEqual(
            self.run_count("--after", "15:45:03.548700", "--before", "15:45:03.548900"), 7
        )
        self.assertEqual(
            self.run_count(
                "--after", "2026-07-27T15:45:03.548700",
                "--before", "2026-07-27T15:45:03.548900",
            ), 7,
        )

    def test_pattern_matches_multiline_block_body(self):
        # "RLC Buffer State" only appears in the SCHED block's continuation
        # line, not its header.
        self.assertEqual(self.run_count("--pattern", "RLC Buffer State"), 1)
        output = self.run_search("--pattern", "RLC Buffer State")
        self.assertIn("[SCHED   ] [D] [    91.0] Processed slot events pci=1:", output)
        self.assertIn("- RLC Buffer State: ue=0 lcid=0 pending_bytes=302", output)

    def test_no_match_message_on_stderr(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), self.tmpdir.name, "--pattern", "no such text"],
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout, "")
        self.assertIn("no entries match the filters", proc.stderr)


if __name__ == "__main__":
    unittest.main()
