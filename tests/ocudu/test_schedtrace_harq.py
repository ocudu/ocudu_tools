#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for schedtrace_harq.py, which imports the parsers package of this repo through the symlink in
skills/analyze-ran-log/scripts/_lib. The grouping itself is tested in tools/parsers/tests/schedtrace.

Usage:
    python3 test_schedtrace_harq.py
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parents[1]
SCRIPT = ROOT / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "schedtrace_harq.py"
# Trace of a cell start, a slot of 10 per frame, slot_tx 12345, with a PDSCH of 0x4601 on HARQ process 3 and a PUSCH
# of 0x4602 on process 0, and a cell stop.
TRACE = ROOT / "tools" / "parsers" / "tests" / "schedtrace" / "data" / "schedtrace_cell0.bin"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class SchedtraceHarqTest(unittest.TestCase):
    def test_transaction(self):
        res = run(str(TRACE), "--rnti", "4601", "--harq-id", "3", "--slot", "1234.5")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DL rnti=0x4601 ue=0 harq_id=3 tbs=12345", res.stdout)
        self.assertIn("PDSCH", res.stdout)

    def test_filters(self):
        res = run(str(TRACE.parent), "--rnti", "0x4602", "--harq-id", "0", "--dir", "ul")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("UL rnti=0x4602", res.stdout)
        self.assertIn("outcome unknown", res.stdout)
        res = run(str(TRACE), "--rnti", "4601", "--harq-id", "3", "--slot", "1234.6")
        self.assertIn("no transactions match", res.stdout)

    def test_failed(self):
        res = run(str(TRACE), "--failed")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no HARQ-ACK report by the end of the events", res.stdout)
        self.assertNotIn("0x4602", res.stdout)

    def test_usage_errors(self):
        self.assertEqual(run(str(TRACE)).returncode, 2)
        self.assertEqual(run(str(TRACE), "--failed", "--dir", "ul").returncode, 2)
        self.assertEqual(run(str(TRACE), "--rnti", "4601", "--harq-id", "3", "--slot", "12").returncode, 2)
        res = run(str(TRACE.parent / "missing"), "--failed")
        self.assertEqual(res.returncode, 1)


if __name__ == "__main__":
    unittest.main()
