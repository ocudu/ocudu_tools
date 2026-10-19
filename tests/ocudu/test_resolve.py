#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the classification of OCUDU component dirs by scripts/resolve.py and of the inventory of
scripts/ocudu/resolve.py.

Usage:
    python3 test_resolve.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPTS = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts"
DU_LOG = "2026-09-29T21:31:50.000000 [DU      ] [I] Built in RelWithDebInfo mode\n"


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # The component dir of a split DU: its f1ap.pcap beside the run subdir with the DU log.
        self.comp = Path(self.tmp.name) / "test_cu_2du[x]" / "ocudu-du-1-1"
        self.run_dir = self.comp / "2026-09-29_21-31-50"
        self.run_dir.mkdir(parents=True)
        (self.comp / "f1ap.pcap").write_bytes(b"x")
        for name in ("rlc.pcap", "ocudu_du.yml", "metrics.json"):
            (self.run_dir / name).write_text("x")
        (self.run_dir / "du.log").write_text(DU_LOG)
        (self.run_dir / "stdout_du.log").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_component_dir_with_a_pcap_is_ocudu(self):
        res = subprocess.run([sys.executable, str(SCRIPTS / "resolve.py"), str(self.comp)],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(res.stdout.startswith("kind: ocudu"), res.stdout)

    def test_dir_of_pcaps_is_pcap(self):
        # Under its own name, as other resolve.py scripts load as module resolve.
        spec = importlib.util.spec_from_file_location("top_resolve", SCRIPTS / "resolve.py")
        resolve = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolve)
        self.assertEqual(resolve.classify(self.run_dir.parent.parent)[0], "ocudu")
        (self.run_dir / "du.log").unlink()
        self.assertEqual(resolve.classify(self.comp)[0], "pcap")
        # Only a Retina component dir gives up kind pcap for the run below it.
        mix = Path(self.tmp.name) / "mix"
        (mix / "old_run").mkdir(parents=True)
        for name in ("gnb_f1ap.pcap", "gnb_ngap.pcap"):
            (mix / name).write_bytes(b"x")
        (mix / "old_run" / "gnb.log").write_text(DU_LOG)
        self.assertEqual(resolve.classify(mix)[0], "pcap")
        (self.comp / "old_run").mkdir()
        (self.comp / "old_run" / "du.log").write_text(DU_LOG)
        self.assertEqual(resolve.classify(self.comp)[0], "pcap")

    def test_stdout_of_a_split_app(self):
        res = subprocess.run([sys.executable, str(SCRIPTS / "ocudu" / "resolve.py"), str(self.comp), "--json"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertEqual((report["present"], report["missing"]),
                         (["du.log", "stdout_du.log", "metrics.json", "ocudu_du.yml"], []))


if __name__ == "__main__":
    unittest.main()
