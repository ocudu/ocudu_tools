#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the run subdirs that correlate/resolve.py inventories.

Usage:
    python3 test_resolve.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "resolve.py"

MME_LOG = """\
# ltemme version 2026-06-12, gcc 15.2.0, glibc 2.17, sse4, Linux x86_64 6.8.0-139-generic
# Started on 2026-09-29 21:24:07
"""


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.tmp.name) / "test_gnb[multiue.capacity.udp_ul]"
        # Retina layout of the Amarisoft 5GC: one run subdir per application.
        self.mme = self.test_dir / "amarisoft-mme-1"
        self.mme_run = self.mme / "mme" / "2026-09-29_21-24-07"
        self.mme_run.mkdir(parents=True)
        (self.mme_run / "mme.log").write_text(MME_LOG)
        (self.mme_run / "amarisoft_mme.cfg").write_text("{}\n")
        (self.mme_run / "metrics.json").write_text("[]\n")
        # A later ims/ run without a core network log.
        (self.mme / "ims" / "2026-09-29_21-24-09").mkdir(parents=True)
        (self.mme / "agent-log-2026-09-29_21-23-56.log").write_text("")
        ue_run = self.test_dir / "amarisoft-ue-1" / "2026-09-29_21-24-10"
        ue_run.mkdir(parents=True)
        (ue_run / "ue.log").write_text("# Started on 2026-09-29 21:24:10\n")

    def tearDown(self):
        self.tmp.cleanup()

    def mme_entry(self, path: Path) -> dict:
        res = run(str(path), "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        comps = json.loads(res.stdout)["components"]
        return next(c for c in comps if c["component"] == "amarisoft-mme-1")

    def test_mme_run_one_level_down(self):
        for path in (self.test_dir, self.mme, self.mme_run):
            entry = self.mme_entry(path)
            self.assertEqual(entry["run_dir"], str(self.mme_run.resolve()))
            self.assertEqual(entry["logs"], ["mme.log"])
            self.assertEqual(entry["configs"], ["amarisoft_mme.cfg"])
            self.assertTrue(entry["metrics"])
            self.assertEqual(entry["clock_anchor"]["started_on"], "2026-09-29")

    def test_text_names_the_nested_run_subdir(self):
        res = run(str(self.test_dir))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("run subdir : mme/2026-09-29_21-24-07", res.stdout)
        self.assertIn("run subdir : 2026-09-29_21-24-10", res.stdout)


if __name__ == "__main__":
    unittest.main()
