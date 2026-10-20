#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the logs and NGAP pcap picked by align_clocks.py.

Usage:
    python3 test_align_clocks.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "align_clocks.py"


def load_script():
    # The sibling modules, e.g. utils, share names with those of other script dirs, so they leave sys.modules.
    path, modules = list(sys.path), set(sys.modules)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location("align_clocks", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
    finally:
        sys.path[:] = path
        for m in set(sys.modules) - modules:
            del sys.modules[m]
    return script
CU_LOG = "2026-04-29T14:27:20.000000 [NGAP    ] [I] Tx PDU: NGSetupRequest\n"
DU_LOG = "2026-04-29T14:27:21.281595 [PHY     ] [I] [  123.14] PUSCH: rnti=0x460b tbs=11 crc=OK sinr=97.0dB\n"
UE_LOG = "14:27:21.273 [PHY] UL 0035 00 460b  123.14 PUSCH: harq=0 tb_len=11\n"


class AlignClocksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.runs = {}
        for comp, name, text in (("ocudu-cu-1", "cu.log", CU_LOG), ("ocudu-du-1-1", "du.log", DU_LOG),
                                 ("amarisoft-ue-1", "ue.log", UE_LOG)):
            run = self.dir / "test" / comp / "2026-04-29_14-27-00"
            run.mkdir(parents=True)
            (run / name).write_text(text)
            self.runs[name] = run / name

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=self.dir)

    def test_split_run_checks_the_cu_log(self):
        res = self.run_script(str(self.dir / "test"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"PHY log  : {self.runs['du.log']}", res.stdout)
        self.assertIn(f"NGAP log : {self.runs['cu.log']}", res.stdout)
        self.assertIn(f"skipped: no *ngap.pcap beside {self.runs['cu.log']}", res.stdout)
        self.assertIn("first co-identified PUSCH", res.stdout)

    def test_explicit_logs_without_run_dir(self):
        res = self.run_script("--gnb", str(self.runs["du.log"]), "--ue", str(self.runs["ue.log"]))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("first co-identified PUSCH", res.stdout)
        # A DU log writes no NGAP, so it is not checked against a pcap.
        self.assertIn("[log <-> pcap]  skipped: no run dir and du.log writes no NGAP (a DU log); pass --cu", res.stdout)
        self.assertIn("Conclusion: the gNB or CU log", res.stdout)
        res = self.run_script("--ue", str(self.runs["ue.log"]))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[log <-> pcap]  skipped: no run dir and no gNB log or --cu", res.stdout)
        self.assertIn("[UE <-> gNB]  skipped: no gNB or DU log (--gnb)", res.stdout)

    def test_du_log_decided_by_content(self):
        # A DU log named gnb.log, by its banner, and a gNB log named du.log, by its NGAP lines.
        du = self.dir / "gnb.log"
        du.write_text("2026-04-29T14:27:20.000000 [GNB     ] [I] ==== DU started ===\n" + CU_LOG + DU_LOG)
        res = self.run_script("--gnb", str(du), "--ue", str(self.runs["ue.log"]))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("skipped: no run dir and gnb.log writes no NGAP (a DU log); pass --cu", res.stdout)
        gnb = self.dir / "du.log"
        gnb.write_text(CU_LOG + DU_LOG)
        res = self.run_script("--gnb", str(gnb), "--ue", str(self.runs["ue.log"]))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"NGAP log : {gnb}", res.stdout)

    def test_ue_log_without_phy(self):
        self.runs["ue.log"].write_text("# Started on 2026-04-29 14:27:19\n14:27:21.273 [RRC] UL 0035 setup\n")
        res = self.run_script(str(self.dir / "test"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[UE <-> gNB]  no PHY lines in ue.log", res.stdout)

    def test_run_subdir_is_inventoried(self):
        # A newer run of the DU has no PHY, so the given older one must be read.
        newer = self.runs["du.log"].parent.parent / "2026-04-30_09-00-00"
        newer.mkdir()
        (newer / "du.log").write_text("2026-04-30T09:00:01.000000 [DU      ] [I] DU started\n")
        res = self.run_script(str(self.runs["du.log"].parent))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"PHY log  : {self.runs['du.log']}", res.stdout)

    def test_cu_log_alone(self):
        # The current dir is not a run and is not searched; the relative --cu path is shown absolute.
        cwd = self.dir / "elsewhere"
        cwd.mkdir()
        res = subprocess.run([sys.executable, str(SCRIPT), "--cu", os.path.relpath(self.runs["cu.log"], cwd)],
                             capture_output=True, text=True, cwd=cwd)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"NGAP log : {self.runs['cu.log']}", res.stdout)
        self.assertIn(f"skipped: no *ngap.pcap beside {self.runs['cu.log']}", res.stdout)
        self.assertIn("Conclusion: cu.log and the pcaps", res.stdout)

    def test_labels_aligned(self):
        (self.runs["cu.log"].parent / "ngap.pcap").write_bytes(b"")
        script = load_script()
        out = io.StringIO()
        with mock.patch.object(script, "first_ngap_frame_epoch", return_value=1777472840.0), \
                contextlib.redirect_stdout(out):
            self.assertEqual(script.main([str(self.dir / "test")]), 0)
        self.assertIn("   cu.log : 2026-04-29T14:27:20.000000\n   pcap   : 2026-04-29T14:27:20", out.getvalue())
        self.assertIn("-> cu.log and pcap share UTC", out.getvalue())


if __name__ == "__main__":
    unittest.main()
