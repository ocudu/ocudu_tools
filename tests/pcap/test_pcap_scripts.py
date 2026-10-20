#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the truncation hint of summary.py and of the errors and messages of correlate_run.py.

Usage:
    python3 test_pcap_scripts.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
PCAP = TEST_DIR.parents[1] / "tools" / "parsers" / "tests" / "pcap" / "f1ap_pcap_test.pcap"
# Two UEs.
REEST_PCAP = PCAP.with_name("f1ap_reestablishment_test.pcap")
SCRIPTS_DIR = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "pcap"


def load_script(name: str):
    # The sibling modules, e.g. utils, share names with those of other script dirs, so they leave sys.modules.
    path, modules = list(sys.path), set(sys.modules)
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
    finally:
        sys.path[:] = path
        for m in set(sys.modules) - modules:
            del sys.modules[m]
    return script


@unittest.skipUnless(shutil.which("tshark"), "tshark not installed")
class TruncationHintTest(unittest.TestCase):
    def test_hint_names_the_flag_of_the_caller(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "f1ap_ue_ids.py"), str(REEST_PCAP), "--limit", "1"],
                              capture_output=True, text=True, check=True)
        self.assertIn("(use --limit 0 to show all)", proc.stdout)
        summary = load_script("summary")
        out = io.StringIO()
        pcaps = {"f1ap": REEST_PCAP, "ngap": None, "e1ap": None}
        with mock.patch.object(summary, "is_run_dir", return_value=True), \
                mock.patch.object(summary, "find_pcaps", return_value=pcaps), \
                mock.patch.object(summary.pcap_overview, "main", return_value=0), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(summary.main([str(REEST_PCAP.parent), "--ue-limit", "1"]), 0)
        self.assertIn("(use --ue-limit 0 to show all)", out.getvalue())


class CorrelateRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ("f1ap.pcap", "ngap.pcap"):
            shutil.copy(PCAP, self.dir / name)
        self.env = {**os.environ, "CLAUDE_CODE_TMPDIR": str(self.dir / "cache")}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPTS_DIR / "correlate_run.py"), str(self.dir), *args],
                              capture_output=True, text=True, env=env or self.env)

    def test_without_tshark(self):
        res = self.run_script(env={**self.env, "PATH": str(self.dir / "nowhere")})
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("error: no pcap read; ngap: tshark not found on PATH; f1ap: tshark not found on PATH", res.stderr)
        self.assertNotIn("no events", res.stdout)

    @unittest.skipUnless(shutil.which("tshark"), "tshark not installed")
    def test_window_without_around_and_empty_message(self):
        res = self.run_script("--ue", "99999", "--window-ms", "5")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("warning: --window-ms applies only with --around; ignored", res.stderr)
        self.assertEqual(res.stdout.strip(), "(no events of UE 99999)")
        res = self.run_script("--around", "1", "--ue", "1")
        self.assertEqual(res.stdout.strip(), "(no events in window of UE 1)")
        self.assertNotIn("--window-ms", res.stderr)


if __name__ == "__main__":
    unittest.main()
