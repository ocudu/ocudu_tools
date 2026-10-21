#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for schedtrace_ue_metrics.py, which imports the parsers package of this repo through the symlink in
skills/analyze-ran-log/scripts/_lib. The metrics themselves are tested in tools/parsers/tests/schedtrace.

Usage:
    python3 test_schedtrace_ue_metrics.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parents[1]
SCRIPT = ROOT / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "schedtrace_ue_metrics.py"
# Trace of a cell start, a slot with grants and reports of UEs 0x4601 and 0x4602, and a cell stop.
TRACE = ROOT / "tools" / "parsers" / "tests" / "schedtrace" / "data" / "schedtrace_cell0.bin"


def load_script():
    spec = importlib.util.spec_from_file_location("schedtrace_ue_metrics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


    def test_msg3_ue_index(self):
        # A Msg3 PUSCH of 0x4603, whose UE has no DU UE index, and one of 0x4601, whose UE gets index 0.
        script = load_script()
        agg = script.ue_metrics.Aggregator()
        msg3 = (0, 11, 3, 1, 0, 0, 0)
        items = {"dl_grants": [], "ul_grants": [("0x4603", None, *msg3), ("0x4601", None, *msg3)], "harq_acks": [],
                 "csi_reports": [], "srs": [("0x4601", 0)]}
        agg.add(0, {"timestamp_us": 0}, items)
        res = {"agg": agg, "t_min": 0.0, "t_max": 0.0, "slot_s": 0.001, "nof_slots": 1, "latency_sum": 0,
               "latency_max": 0, "kept": [("2026-01-01T00:00:00.000000", *row) for row in items["ul_grants"]]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            script.print_summary(res)
            script.print_grants(res, "ul", 10)
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split()[:2] for line in lines if line.startswith("0x")], [["0x4601", "0"], ["0x4603", "-"]])
        self.assertEqual([line.split()[1:3] for line in lines if line.startswith("00:00:00")],
                         [["0x4601", "-"], ["0x4603", "-"]])

    def test_zero_tail(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(TRACE.read_bytes() + bytes(4096))
            for workers in ("1", "2"):
                res = run(str(path), "--summary", "-j", workers)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("slots=1", res.stdout)
                self.assertIn("corrupt schedtrace event size 0", res.stderr)

    def test_empty_trace(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(b"")
            res = run(str(path))
        self.assertEqual(res.returncode, 1)
        self.assertIn(f"ERROR: {path}: cannot mmap an empty file", res.stderr)


if __name__ == "__main__":
    unittest.main()
