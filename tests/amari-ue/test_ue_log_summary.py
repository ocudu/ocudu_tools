#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the input resolution of ue_log_summary.py and resolve.py, and of the error count of ue_log_summary.py.

The fixture lines are real amari-ue log lines.

Usage:
    python3 test_ue_log_summary.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "analyze-ran-log" / "scripts" / "amari-ue"

UE_LOG = """\
# lteue version 2026-06-12, gcc 15.2.0, glibc 2.17, avx2, Linux x86_64 6.17.0-41-generic
# Started on 2026-09-29 21:04:13
21:04:13.636 [NAS] -  0001 New state : 5GMM-NULL   CM-IDLE
21:04:17.850 [MAC] -  0007 00 Contention resolution failed or timer expired
21:04:17.850 [RRC] -  0007  - Random access failure
21:04:18.001 [RRC] -  0001  - PDCP reestablish for SRB 1 without reconfigurationWithSync or after RRC Resume; ignore it
21:04:18.002 [PHY] DL 0001 00 4601    16.1 PDSCH: harq=0 prb=1:11 symb=2:12 CW0: tb_len=111 mod=2 rv_idx=0 cr=0.37 crc=FAIL snr=5.5 epre=-47.1
# Ended on 2026-09-29 21:04:51
"""


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True)


class UeLogSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, rel: str, name: str = "ue.log") -> Path:
        d = self.root / rel
        d.mkdir(parents=True)
        (d / name).write_text(UE_LOG)
        return d / name

    def test_named_file_is_parsed(self):
        ue0 = self.make_run("cap", "ue0.log")
        res = run("ue_log_summary.py", str(ue0))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("UE log        : ue0.log", res.stdout)
        self.assertIn("5GMM-NULL CM-IDLE", res.stdout)
        res = run("resolve.py", str(ue0), "--json")
        self.assertEqual(json.loads(res.stdout)["present"], ["ue0.log"])

    def test_dir_without_ue_log_is_an_error(self):
        self.make_run("cap", "ue0.log")
        for script in ("ue_log_summary.py", "resolve.py"):
            res = run(script, str(self.root / "cap"))
            self.assertEqual(res.returncode, 1)
            self.assertIn("No ue.log found", res.stdout + res.stderr)
            self.assertIn("found ue0.log", res.stdout + res.stderr)

    def test_non_log_file_is_rejected(self):
        bad = self.root / "ue.log.html"
        bad.write_text("<html><body>x</body></html>\n")
        res = run("ue_log_summary.py", str(bad))
        self.assertEqual(res.returncode, 1)
        self.assertIn("does not look like an Amarisoft UE log", res.stderr)

    def test_error_lines_are_failure_records(self):
        res = run("ue_log_summary.py", str(self.make_run("run")))
        # The two failure records; neither the PHY crc=FAIL nor the "without reconfigurationWithSync" note.
        self.assertIn("Error log lines    : 2", res.stdout)
        self.assertIn("PHY CRC failures   : 1", res.stdout)
        self.assertIn("Handovers (sync)   : 0", res.stdout)

    def test_components_are_distinct_paths(self):
        # Same component name under two test dirs, and amarisoft-ue-1 beside amarisoft-ue-10.
        self.make_run("test_gnb[a]/amarisoft-ue-1/2026-09-29_21-04-12")
        self.make_run("test_gnb[b]/amarisoft-ue-1/2026-09-29_21-04-12")
        self.make_run("test_gnb[b]/amarisoft-ue-10/2026-09-29_21-04-12")
        res = run("resolve.py", str(self.root), "--json")
        report = json.loads(res.stdout)
        self.assertEqual(report["components"], [
            "test_gnb[a]/amarisoft-ue-1", "test_gnb[b]/amarisoft-ue-1", "test_gnb[b]/amarisoft-ue-10"])
        self.assertEqual(report["resolved_component"], "test_gnb[b]/amarisoft-ue-10")
        res = run("resolve.py", str(self.root / "test_gnb[b]" / "amarisoft-ue-1"), "--json")
        self.assertNotIn("components", json.loads(res.stdout))


if __name__ == "__main__":
    unittest.main()
