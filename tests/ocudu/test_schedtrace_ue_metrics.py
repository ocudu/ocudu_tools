#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for schedtrace_ue_metrics.py, which imports the parsers package of this repo through the symlink in
skills/analyze-ran-log/scripts/_lib. The metrics themselves are tested in tools/parsers/tests/schedtrace.

Usage:
    python3 test_schedtrace_ue_metrics.py
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parents[1]
SCRIPT = ROOT / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "schedtrace_ue_metrics.py"
# Trace of a cell start, a slot with grants and reports of UEs 0x4601 and 0x4602, and a cell stop.
TRACE = ROOT / "tools" / "parsers" / "tests" / "schedtrace" / "data" / "schedtrace_cell0.bin"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class SchedtraceUeMetricsTest(unittest.TestCase):
    def test_summary_and_periods(self):
        res = run(str(TRACE), "-j", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("slots=1", res.stdout)
        self.assertIn("0x4601", res.stdout)
        self.assertIn("0x4602", res.stdout)
        self.assertIn("dl_sched_Mbps", res.stdout)

    def test_ue_filter_and_grants(self):
        res = run(str(TRACE.parent), "--rnti", "17921", "--grants", "dl")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("12345", res.stdout)
        self.assertNotIn("0x4602", res.stdout)

    def test_rnti_in_any_form(self):
        for rnti in ("4601", "0x4601", "17921"):
            res = run(str(TRACE), "--rnti", rnti, "--summary")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("0x4601", res.stdout, rnti)
            self.assertNotIn("0x4602", res.stdout, rnti)

    def test_filter_without_ues(self):
        res = run(str(TRACE), "--rnti", "0x4700")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no grants or reports of the filtered UEs in the window", res.stdout)

    def test_non_finite_period(self):
        for period in ("inf", "nan", "0"):
            res = run(str(TRACE), "--period", period)
            self.assertEqual(res.returncode, 2, period)
            self.assertIn("--period must be finite and positive", res.stderr, period)

    def test_window_with_date_and_space(self):
        # A date and time joined by a space, and a date, bound the slot of 2023-11-14T22:13:20 as ISO times.
        for before, nof_slots in (("2023-11-14 22:13:21", 1), ("2023-11-14", 1), ("2023-11-13", 0)):
            res = run(str(TRACE), "--before", before, "--summary")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("slots=1" if nof_slots else "no slots in the window", res.stdout, before)

    def test_window_without_slots(self):
        res = run(str(TRACE), "--after", "23:00")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no slots in the window", res.stdout)

    def test_invalid_rnti(self):
        res = run(str(TRACE), "--rnti", "zz")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid RNTI", res.stderr)


if __name__ == "__main__":
    unittest.main()
