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

    def test_no_note_of_processes_without_cu_cp(self):
        # By their banner.
        for logger, app in (("CU-UP", "CU-UP"), ("APP", "DU low")):
            banner = f"2026-09-29T21:31:58.240299 [{logger:<8}] [I] ==== {app} started ===\n"
            self.assertNotIn("NOTE", self.summary(QUIET_LOG + banner))
        # Without a banner, e.g. of a rotated log, by their loggers.
        du = ('2026-09-29T21:32:20.000000 [DU-MNG  ] [I] ue=0 rnti=0x4601 proc="UE Create": Procedure started...\n'
              "2026-09-29T21:32:20.100000 [DU-F1   ] [I] Tx PDU du=0 ue=0: InitialULRRCMessageTransfer\n")
        self.assertNotIn("NOTE", self.summary(QUIET_LOG + du))
        cu_up = "2026-09-29T21:32:20.000000 [CU-UP-E1] [I] Rx PDU: BearerContextSetupRequest\n"
        self.assertNotIn("NOTE", self.summary(QUIET_LOG + cu_up))
        # A CU-CP logger is of a CU-CP.
        cu_cp = "2026-09-29T21:32:20.200000 [CU-CP-F1] [I] Rx PDU du=0: InitialULRRCMessageTransfer\n"
        self.assertIn("NOTE", self.summary(QUIET_LOG + du + cu_cp))

    def test_stdout_of_the_component(self):
        log = Path(self.tmp.name) / "du.log"
        log.write_text(QUIET_LOG)
        stdout = "--== OCUDU DU (commit abc123) ==--\n==== DU started ===\n"
        (Path(self.tmp.name) / "stdout_du.log").write_text(stdout)
        (Path(self.tmp.name) / "stdout_mme.log").write_text("other\n")
        res = subprocess.run([sys.executable, str(SCRIPT), self.tmp.name], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("stdout banner   : abc123", res.stdout)
        self.assertIn("stdout marker       : started but no `Stopping...` line", res.stdout)

    def test_handovers_at_info(self):
        ho = ('2026-07-06T17:14:11.000000 [CU-CP   ] [I] ue=1: "Intra CU Handover Target Routine" finished '
              "successfully\n")
        self.assertIn("Handovers completed : 1", self.summary(QUIET_LOG + VERBOSE_LINES + ho))

    def test_initial_context_setup_of_the_ues_of_an_rrc_setup(self):
        # CU-CP UEs of a handover, a reestablishment and an RRC resume, which run no Initial Context Setup.
        others = ("2026-07-06T17:14:10.600000 [CU-CP   ] [I] ue=1 c-rnti=0x4602: UE created\n"
                  '2026-07-06T17:14:10.700000 [CU-CP   ] [I] ue=1: "Intra CU Handover Target Routine" finished '
                  "successfully\n"
                  "2026-07-06T17:14:10.800000 [CU-CP   ] [I] ue=2 c-rnti=0x4603: UE created\n"
                  "2026-07-06T17:14:10.800100 [RRC     ] [I] ue=2 c-rnti=0x4603: CCCH UL rrcReestablishmentRequest\n"
                  "2026-07-06T17:14:10.900000 [CU-CP   ] [I] ue=3 c-rnti=0x4604: UE created (RRC Resume)\n")
        ics = ('2026-07-06T17:14:10.450000 [CU-CP   ] [I] ue=0: "Initial Context Setup Routine" finished '
               "successfully\n")
        out = self.summary(QUIET_LOG + VERBOSE_LINES + others + ics)
        self.assertIn("Initial Context Setup OK : 1/1", out)
        self.assertNotIn("Initial Context Setup incomplete", out)
        # A UE of an RRC setup, at debug, without Initial Context Setup.
        setup = ("2026-07-06T17:14:11.000000 [RRC     ] [D] ue=4 c-rnti=0x4605: Rx SRB1 DCCH UL rrcSetupComplete "
                 "(96 B)\n"
                 "2026-07-06T17:14:11.000100 [RRC     ] [D] ue=4 c-rnti=0x4605: Containerized rrcSetupComplete: [\n")
        out = self.summary(QUIET_LOG + VERBOSE_LINES + others + ics + setup)
        self.assertIn("Initial Context Setup OK : 1/2", out)
        self.assertIn("Initial Context Setup incomplete: 1/2", out)
        self.assertIn("Initial Context Setup incomplete: 0/1", self.summary(QUIET_LOG + VERBOSE_LINES))
        # CU-CP, which logs the completions, below info.
        rrc_only = VERBOSE_LINES.splitlines(keepends=True)[0]
        self.assertNotIn("Initial Context Setup incomplete", self.summary(QUIET_LOG + rrc_only))

    def test_note_of_procedure_loggers_at_warning(self):
        cfg = Path(self.tmp.name) / "ocudu_gnb.yml"
        cfg.write_text("log:\n  f1ap_level: warning\n")
        self.assertNotIn("NOTE", self.summary(QUIET_LOG + VERBOSE_LINES))
        cfg.write_text("log:\n  f1ap_level: warning\n  e1ap_level: warning\n")
        self.assertIn("NOTE: e1ap logged at 'warning'", self.summary(QUIET_LOG + VERBOSE_LINES))

    def test_metrics_rows_leave_out_the_worker_lines(self):
        out = self.summary(QUIET_LOG)
        self.assertIn("Metrics rows in gnb.log : 1", out)
        self.assertIn("Scheduler UE ue=0 pci=1 rnti=0x4601 metrics: dl_mcs=27", out)
        self.assertNotIn("Stopped the executor", out)


if __name__ == "__main__":
    unittest.main()
