#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the inputs and filters of ue_table.py, without tshark.

Usage:
    python3 test_ue_table.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "ue_table.py"


def load_script():
    spec = importlib.util.spec_from_file_location("ue_table", SCRIPT)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


class UeTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name: str) -> Path:
        path = self.dir / name
        path.write_text("")
        return path

    def test_log_of_a_pcap_or_config(self):
        log = self.touch("gnb.log")
        for name in ("gnb_f1ap.pcap", "gnb.yml"):
            self.assertEqual(self.script.resolve_inputs(str(self.touch(name)), None), (self.dir, log))
        other = self.touch("my.log")
        self.assertEqual(self.script.resolve_inputs(str(other), None), (self.dir, other))

    def test_log_of_no_standard_name(self):
        log = self.touch("my.txt")
        with self.assertRaises(FileNotFoundError):
            self.script.resolve_inputs(str(self.dir), None)
        self.assertEqual(self.script.resolve_inputs(str(self.dir), str(log)), (self.dir, log))
        self.assertEqual(self.script.resolve_inputs("/nonexistent", str(log)), (self.dir, log))
        with self.assertRaises(FileNotFoundError):
            self.script.resolve_inputs(str(self.touch("gnb_f1ap.pcap")), None)

    def test_errors(self):
        def run(*args: str) -> str:
            res = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
            self.assertEqual(res.returncode, 1, args)
            self.assertTrue(res.stderr.startswith("error: "), res.stderr)
            return res.stderr

        run("/nonexistent")
        run(str(self.dir))
        self.assertIn("/nonexistent.pcap", run(str(self.touch("gnb.log")), "--f1ap", "/nonexistent.pcap"))

    def test_time_bound(self):
        epoch = datetime(2026, 10, 12, 13, 52, 0, 500000, tzinfo=timezone.utc).timestamp()
        for ref, value in (("2026-10-12T13:52:00", "2026-10-12T13:52:00"), ("2026-10-12", "2026-10-12"),
                           ("13:52", "13:52")):
            self.assertEqual(self.script.time_bound(ref, epoch), value)

    def test_rnti_in_any_form(self):
        ids = {"rnti": ["0x4601"]}
        for rnti in ("4601", "0x4601", "17921"):
            self.assertTrue(self.script.matches(ids, [self.script.where_item(f"rnti={rnti}")]), rnti)
        self.assertFalse(self.script.matches(ids, [self.script.where_item("rnti=4602")]))

    def test_invalid_where(self):
        for item, message in (("rnti=zz", "invalid RNTI"), ("rnit=4601", "is not one of"), ("rnti", "takes k=v")):
            res = subprocess.run([sys.executable, str(SCRIPT), str(self.dir), "--where", item], capture_output=True,
                                 text=True)
            self.assertEqual(res.returncode, 2, item)
            self.assertIn(message, res.stderr, item)

    def test_contexts_in_both_sources(self):
        ues = [SimpleNamespace(parts=[("f1ap.pcap", 1), ("gnb.log", 2)]), SimpleNamespace(parts=[("gnb.log", 3)])]
        self.assertEqual(self.script.nof_in_both(ues, Path("f1ap.pcap"), Path("gnb.log")), 1)
        self.assertEqual(self.script.nof_in_both(ues, None, Path("gnb.log")), 0)


if __name__ == "__main__":
    unittest.main()
