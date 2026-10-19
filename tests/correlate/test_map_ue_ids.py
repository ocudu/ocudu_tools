#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the argument check, the staging and the tshark errors of map_ue_ids.py.

Usage:
    python3 test_map_ue_ids.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "map_ue_ids.py"


def load_script():
    # The sibling modules, e.g. utils, share names with those of other script dirs, so they leave sys.modules.
    path, modules = list(sys.path), set(sys.modules)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("map_ue_ids", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
    finally:
        sys.path[:] = path
        for m in set(sys.modules) - modules:
            del sys.modules[m]
    return script


def run_script(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class MapUeIdsTest(unittest.TestCase):
    def test_help(self):
        res = run_script("--help")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("usage:", res.stdout)

    def test_pcaps_below_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d) / "ocudu-du-1-1"
            comp.mkdir()
            (comp / "f1ap.pcap").write_bytes(b"")
            res = run_script(d)
            self.assertEqual(res.returncode, 1)
            self.assertIn("no pcap directly in it; pcaps below: ocudu-du-1-1/f1ap.pcap", res.stderr)
            (comp / "f1ap.pcap").unlink()
            self.assertIn("no pcap in or below it", run_script(d).stderr)

    @unittest.skipUnless(shutil.which("tshark"), "tshark not installed")
    def test_unknown_protocol(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "rlc.pcap").write_bytes(b"")
            res = run_script(str(Path(d) / "rlc.pcap"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("error: cannot detect protocol in", res.stderr)

    def test_directory(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "f1ap.pcap").write_bytes(b"")
            res = subprocess.run([sys.executable, str(SCRIPT), d], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("is not a pcap file; its pcaps: f1ap.pcap", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    @unittest.skipUnless(shutil.which("tshark"), "tshark not installed")
    def test_tshark_failure(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "f1ap.pcap").write_text("not a capture\n")
            res = run_script(str(Path(d) / "f1ap.pcap"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("error: tshark failed on", res.stderr)
        self.assertIn("isn't a capture file", res.stderr)

    def test_symlink_under_tmp_is_staged(self):
        # tshark may only read /tmp, so a link there to a file elsewhere is staged like the file.
        script = load_script()
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "f1ap.pcap"
            link.symlink_to(SCRIPT)
            staged = script._stage_for_tshark(str(link))
            self.assertNotEqual(staged, str(link))
            self.assertEqual(Path(staged).read_bytes(), SCRIPT.read_bytes())
            os.unlink(staged)


if __name__ == "__main__":
    unittest.main()
