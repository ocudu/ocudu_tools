#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the --rnti filter of correlate_radio.py.

Usage:
    python3 test_correlate_radio.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "correlate_radio.py"
GNB_LOG = """\
2026-04-29T14:27:21.281595 [PHY     ] [I] [  123.14] PUSCH: rnti=0x460b tbs=11 crc=KO sinr=97.0dB
2026-04-29T14:27:21.291595 [PHY     ] [I] [  123.15] PUSCH: rnti=0x4601 tbs=11 crc=KO sinr=97.0dB
"""
UE_LOG = "14:27:21.273 [PHY] UL 0035 00 460b  123.14 PUSCH: harq=0 tb_len=11\n"


class CorrelateRadioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "gnb.log").write_text(GNB_LOG)
        (self.dir / "ue.log").write_text(UE_LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, rnti: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), str(self.dir), "--gnb", str(self.dir / "gnb.log"), "--ue",
                               str(self.dir / "ue.log"), "--only", "all", "--rnti", rnti], capture_output=True, text=True)

    def test_rnti_in_any_form(self):
        # 17931 is 0x460b in decimal, as F1AP pcaps hold it, not the hex 0x17931.
        for rnti in ("460b", "0x460B", "17931"):
            res = self.run_script(rnti)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("0x460b   rx-ko/ue-tx", res.stdout, rnti)
            self.assertNotIn("0x4601", res.stdout, rnti)

    def test_invalid_rnti(self):
        res = self.run_script("zz")
        self.assertEqual(res.returncode, 2)
        self.assertIn("invalid RNTI", res.stderr)


if __name__ == "__main__":
    unittest.main()
