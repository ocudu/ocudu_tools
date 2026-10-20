#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for schedtrace_collisions.py, which imports the parsers package of this repo through the symlink in
skills/analyze-ran-log/scripts/_lib. The detection itself is tested in tools/parsers/tests/schedtrace.

Usage:
    python3 test_schedtrace_collisions.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parents[1]
SCRIPT = ROOT / "skills" / "analyze-ran-log" / "scripts" / "ocudu" / "schedtrace_collisions.py"
# Trace of a cell start, a slot and a cell stop, without collisions.
TRACE = ROOT / "tools" / "parsers" / "tests" / "schedtrace" / "data" / "schedtrace_cell0.bin"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class SchedtraceCollisionsTest(unittest.TestCase):
    def test_trace(self):
        res = run(str(TRACE), "-j", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("schedtrace_cell0.bin: 0 collisions", res.stdout)

    def test_directory(self):
        res = run(str(TRACE.parent), "--summary")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("0 collisions", res.stdout)

    def test_trace_without_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            # The 8-byte header of the file format left out, as in traces written before OCUDU versioned it.
            path.write_bytes(TRACE.read_bytes()[8:])
            res = run(str(path))
        self.assertEqual(res.returncode, 1)
        self.assertIn("ERROR:", res.stderr)
        self.assertIn("header is missing", res.stderr)

    def test_traces_without_their_start_event(self):
        with tempfile.TemporaryDirectory() as d:
            header_only, empty = Path(d) / "schedtrace_cell0.bin", Path(d) / "schedtrace_cell1.bin"
            header_only.write_bytes(TRACE.read_bytes()[:8])
            empty.write_bytes(b"")
            res = run(d)
        # Both are reported with their paths, the run going on past the first.
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.stderr.count("ERROR:"), 2, res.stderr)
        self.assertIn(f"ERROR: {header_only}: the trace does not start", res.stderr)
        self.assertIn(f"ERROR: {empty}: cannot mmap an empty file", res.stderr)

    def test_zero_tail(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(TRACE.read_bytes() + bytes(4096))
            for workers in ("1", "2"):
                res = run(str(path), "-j", workers)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("0 collisions", res.stdout)
                self.assertIn("corrupt schedtrace event size 0", res.stderr)

    def test_overlap_text(self):
        spec = importlib.util.spec_from_file_location("schedtrace_collisions", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        cces = script.collisions.Collision("PDCCH-PDCCH", 0, 0, {}, {}, [(2, 2)])
        rects = script.collisions.Collision("PDCCH-PDCCH", 0, 0, {}, {}, [(0, 1, 16, 4)])
        self.assertEqual((script.overlap_text(cces), script.overlap_text(rects)), ("cces=[2,4)", "syms=[0,1) crbs=[16,20)"))

    def test_rnti_in_any_form(self):
        spec = importlib.util.spec_from_file_location("schedtrace_collisions", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        self.assertEqual({script.parse_rnti(r) for r in ("4601", "0x4601", "17921")}, {0x4601})
        res = run(str(TRACE), "--rnti", "zz")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not an RNTI", res.stderr)

    def test_unknown_kind(self):
        res = run(str(TRACE), "--kinds", "PUSCH-PDSCH")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unknown kinds", res.stderr)


if __name__ == "__main__":
    unittest.main()
