#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the procedure filter and the messages of ocudu_ue_proc_latency.py.

Usage:
    python3 test_ocudu_ue_proc_latency.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "ocudu_ue_proc_latency.py"
DU_LOG = """\
2026-04-21T03:21:49.370563 [MAC     ] [D] [   833.5] ue=0 crnti=0x4601 proc="Sched UE Config": started...
2026-04-21T03:21:49.371025 [MAC     ] [I] [   833.6] ue=0 crnti=0x4601 proc="Sched UE Config": successfully finished
"""
CU_LOG = "2026-04-21T03:21:49.370563 [CU-CP   ] [I] ue=0: \"Initial Context Setup Routine\" finished successfully\n"


class OcuduUeProcLatencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "gnb.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, text: str, *args: str) -> str:
        self.log.write_text(text)
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.log), *args], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_filter_in_any_case(self):
        self.assertIn('proc="Sched UE Config"', self.run_script(DU_LOG, "sched"))

    def test_messages_without_procedures(self):
        self.assertIn("no DU UE procedure in the log", self.run_script(CU_LOG))
        self.assertIn("no procedure of the log matches 'removal'; the log has: Sched UE Config",
                      self.run_script(DU_LOG, "removal"))


if __name__ == "__main__":
    unittest.main()
