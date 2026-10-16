#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the procedure counts and their note and the METRICS rows of ocudu_log_summary.py, on logs without a
configuration.

Usage:
    python3 test_ocudu_log_summary.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "ocudu_log_summary.py"
QUIET_LOG = """\
2026-07-06T17:14:03.274755 [GNB     ] [I] Built in RelWithDebInfo mode using commit 5a5d042849 on branch dev
2026-07-06T17:14:10.143952 [METRICS ] [I] Started the executor metrics backend worker
2026-07-06T17:14:11.000000 [METRICS ] Scheduler UE ue=0 pci=1 rnti=0x4601 metrics: dl_mcs=27
2026-07-06T17:14:11.500000 [OFH     ] [W] Late message
2026-07-06T17:14:12.000000 [METRICS ] [I] Stopped the executor metrics backend worker
"""
VERBOSE_LINES = """\
2026-07-06T17:14:10.200000 [RRC     ] [I] ue=0 c-rnti=0x4601: Rx DCCH UL rrcSetupComplete
2026-07-06T17:14:10.300000 [NGAP    ] [I] ue=0: Tx PDU InitialUEMessage
2026-07-06T17:14:10.400000 [CU-CP   ] [I] ue=0 c-rnti=0x4601: UE created
2026-07-06T17:14:10.500000 [CU-CP-E1] [D] Tx PDU BearerContextSetupRequest
"""


class OcuduLogSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "gnb.log"

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, text: str) -> str:
        self.log.write_text(text)
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.log)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_note_of_procedure_loggers_without_info_lines(self):
        out = self.summary(QUIET_LOG)
        self.assertIn("NOTE: no info or debug lines of RRC, NGAP, CU-CP, CU-CP-E1", out)
        self.assertNotIn("NOTE", self.summary(QUIET_LOG + VERBOSE_LINES))

    def test_no_note_without_cu_loggers_or_ues(self):
        # A DU has no CU loggers.
        du = "2026-09-29T21:31:58.240299 [GNB     ] [I] ==== DU started ===\n"
        self.assertNotIn("NOTE", self.summary(QUIET_LOG + du))
        # RRC logs nothing at info without UEs.
        no_ue = "".join(line + "\n" for line in VERBOSE_LINES.splitlines()
                        if "RRC" not in line and "UE created" not in line)
        no_ue += "2026-07-06T17:14:10.400000 [CU-CP   ] [I] Connection to AMF on 127.0.0.1:38412 established\n"
        self.assertNotIn("NOTE", self.summary(QUIET_LOG + no_ue))

    def test_handovers_at_info(self):
        ho = ('2026-07-06T17:14:11.000000 [CU-CP   ] [I] ue=1: "Intra CU Handover Target Routine" finished '
              "successfully\n")
        self.assertIn("Handovers completed : 1", self.summary(QUIET_LOG + VERBOSE_LINES + ho))

    def test_metrics_rows_leave_out_the_worker_lines(self):
        out = self.summary(QUIET_LOG)
        self.assertIn("Metrics rows in gnb.log : 1", out)
        self.assertIn("Scheduler UE ue=0 pci=1 rnti=0x4601 metrics: dl_mcs=27", out)
        self.assertNotIn("Stopped the executor", out)


if __name__ == "__main__":
    unittest.main()
