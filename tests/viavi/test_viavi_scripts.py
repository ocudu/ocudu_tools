#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the VIAVI command-log scripts: resolve.py, viavi_log_summary.py and viavi_log_search.py.

The fixture is assembled from real command-log lines.

Usage:
    python3 test_viavi_scripts.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "analyze-ran-log" / "scripts" / "viavi"

# Ends past midnight; one per-UE GETSTATS record and one COMBINED record, each with timestamped stat rows,
# the COMBINED one followed by a command echo of the same timestamp.
COMMAND_LOG = """\
14/01/26 23:17:29:111 RSET
14/01/26 23:17:29:120 C: RSET 0x00 Ok
14/01/26 23:18:30:104 I: CMPI ORAN_RU ORAN: CU PLANE ACTIVE (Ru 0 on server)
14/01/26 23:19:06:119 I: CMPI L2 Random Access Initiated :UE Id:0 (Connection Establish: Cell Id 1, Dl Freq 33750, SSB Id 0)
14/01/26 23:19:06:140 I: CMPI L2 Random Access Complete :UE Id:0 (TC-RNTI: 0x4601, TimingAdv: 0, PreambleTxCount: 1)
14/01/26 23:19:06:140 I: CMPI MTE 0 NR CONNECTION IND:UE Id:0
14/01/26 23:19:06:463 I: CMPI MTE 0 NR REGISTRATION IND:UE Id:0\r
  Selected PLMN: 00101F\r
  Registration Result: 3GPP Access Only
14/01/26 23:19:19:175 forw mte getstats [1][0][all]
14/01/26 23:19:19:183 C: FORW 0x00 Ok MTE GETSTATS [PHY] [0] [0]
14/01/26 23:19:19:183 UE ID: 0   (Radio Context: 0   Cell ID: 1   DL Freq: 3375.000 MHz)
14/01/26 23:19:19:183 DL-SCH (PCC)
14/01/26 23:19:19:183 Throughput: 46254820  Average: 46681020  Min: 46254820  Max: 47132430
14/01/26 23:19:19:183 [NumTBs]       [NumTBErrors]   [BLER]       [Total Bits]    [Total Bit Errors]   [BER]
14/01/26 23:19:19:183 00000000010284 00000000000002  0.0001944380 00000587533376  -                    -
14/01/26 23:19:19:183 UL-SCH (PCC)
14/01/26 23:19:19:183 Throughput: 01042210  Average: 01097088  Min: 01042210  Max: 01134030
14/01/26 23:19:19:185 WAIT FOR "MTS SCENARIO COMPLETE IND" TIMEOUT 1340
14/01/26 23:59:59:900 I: CMPI RRC UE Id:1: RRC Re-establishment Dropped (nr_rrc_rrce_InitiateReestablishment): Check whether RRC_DISABLE_CONNECTION_REESTABLISHMENT setp enabled or UE not registered
14/01/26 23:59:59:950 I: CMPI MTE 0 NR LOWER LAYER FAILURE IND:UE Id:1
15/01/26 00:00:00:010 I: CMPI MTE 0 NR DEREGISTRATION IND:UE Id:0
15/01/26 00:00:05:050 C: FORW 0x00 Ok MTE GETSTATS [ALL] [COMBINED] [COMBINED]
15/01/26 00:00:05:050 UE ID: 0-32   (Radio Context: 0   Cell ID: 1   DL Freq: 3375.000
15/01/26 00:00:05:050 DL-SCH (PCC)
15/01/26 00:00:05:050 Throughput: 000000000  Average: 985022950  Min: 000000000  Max: 1514786750
15/01/26 00:00:05:050 UL-SCH (PCC)
15/01/26 00:00:05:050 Throughput: 000000000  Average: 020387530  Min: 000000000  Max: 035349480
15/01/26 00:00:05:050 forw mte GetNrBeamStats
15/01/26 00:00:06:000 I: CMPI MTE 0 NR DISCONNECTION IND:UE Id:0
"""

# A log cut before the RSET.
PRE_RSET_LOG = """\
14/01/26 03:17:21:205 I: TMAE 0x0 Information - Installed version of python on your system are: Python 3.8-64
14/01/26 03:17:28:332 #$$PORT 192.168.10.70 5001 5002 5003
14/01/26 03:17:28:336 C: PORT 0x00 ok. 
14/01/26 03:17:28:337 #$$CONNECT
14/01/26 03:17:29:106 C: CONNECT 0x00 ok. Waiting for User to Configure Test Mobile 
"""


def write_txt(d: Path, name: str = "260114_231729_Command_Log000.txt", text: str = COMMAND_LOG) -> Path:
    p = d / name
    p.write_bytes(text.encode())
    return p


def write_zip(d: Path, name: str = "260114_231729_Command_Log000.zip", text: str = COMMAND_LOG) -> Path:
    p = d / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name[:-4] + ".txt", text)
    return p


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True)


class ViaviScriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, path: Path) -> str:
        res = run("viavi_log_summary.py", str(path))
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def count(self, path: Path, *args: str) -> int:
        res = run("viavi_log_search.py", str(path), *args, "--count")
        self.assertEqual(res.returncode, 0, res.stderr)
        return int(res.stdout)

    def test_summary_splits_per_ue_and_combined_records(self):
        out = self.summary(write_txt(self.dir))
        self.assertIn("GETSTATS dumps    : 2  (1 per-UE, 1 COMBINED stat records)", out)
        per_ue, combined = out.split("[per-UE, max across records]")[1].split("[COMBINED, all UEs, max across records]")
        self.assertIn("Peak DL avg tput : 46681020", per_ue)
        self.assertIn("Max DL-SCH BLER  : 0.0002", per_ue)
        self.assertIn("Peak DL avg tput : 985022950", combined)

    def test_summary_events(self):
        out = self.summary(write_txt(self.dir))
        self.assertIn("RUs active  : Ru 0", out)
        self.assertIn("LOWER LAYER FAILURE IND: 1", out)
        self.assertIn("Re-establishment Dropped   : 1", out)
        self.assertIn("! 1 NR LOWER LAYER FAILURE IND [UE Ids: 1]", out)
        self.assertIn("Duration    : 42m36s", out)

    def test_zip_read_in_place(self):
        out = self.summary(write_zip(self.dir))
        self.assertIn("(read from .zip)", out)
        self.assertIn("REGISTRATION IND      : 1", out)

    def test_directory_takes_newest_basename(self):
        # An older .txt does not hide a newer .zip; a .txt wins over its own .zip.
        write_txt(self.dir, "260101_000000_Command_Log000.txt")
        newest = write_zip(self.dir)
        res = run("resolve.py", str(self.dir))
        self.assertIn(f"log:     {newest}", res.stdout)
        txt = write_txt(self.dir)
        res = run("resolve.py", str(self.dir))
        self.assertIn(f"log:     {txt}", res.stdout)

    def test_resolve_accepts_pre_rset_log(self):
        res = run("resolve.py", str(write_txt(self.dir, text=PRE_RSET_LOG)))
        self.assertEqual(res.returncode, 0, res.stdout)

    def test_broken_zips_are_errors(self):
        empty = self.dir / "e_Command_Log000.zip"
        zipfile.ZipFile(empty, "w").close()
        truncated = write_zip(self.dir)
        truncated.write_bytes(truncated.read_bytes()[:200])
        for p in (empty, truncated):
            res = run("resolve.py", str(p))
            self.assertEqual(res.returncode, 1, res.stdout)
            self.assertIn("BAIL — cannot read", res.stdout)
            for script in ("viavi_log_summary.py", "viavi_log_search.py"):
                res = run(script, str(p))
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertTrue(res.stderr.startswith("Error: "), res.stderr)

    def test_search_getstats_record_takes_its_rows(self):
        log = write_txt(self.dir)
        res = run("viavi_log_search.py", str(log), "--pattern", "UE ID: 0 ")
        self.assertIn("Average: 46681020", res.stdout)
        self.assertNotIn("WAIT FOR", res.stdout)
        self.assertEqual(self.count(log, "--pattern", "Average: 985022950"), 1)
        res = run("viavi_log_search.py", str(log), "--pattern", "GetNrBeamStats")
        self.assertEqual(res.stdout.strip(), "15/01/26 00:00:05:050 forw mte GetNrBeamStats")

    def test_search_ue_excludes_combined_range(self):
        log = write_txt(self.dir)
        self.assertEqual(self.count(log, "--ue", "0"), 7)
        self.assertEqual(self.count(log, "--ue", "1"), 2)

    def test_search_event(self):
        log = write_txt(self.dir)
        self.assertEqual(self.count(log, "--event", "NR REGISTRATION IND"), 1)
        self.assertEqual(self.count(log, "--event", "REGISTRATION IND"), 2)

    def test_search_window_across_midnight(self):
        log = write_txt(self.dir)
        self.assertEqual(self.count(log), 18)
        self.assertEqual(self.count(log, "--after", "23:59:59:000"), 7)
        self.assertEqual(self.count(log, "--after", "23:59:59", "--before", "00:00:01"), 3)
        self.assertEqual(self.count(log, "--before", "00:00:00:010"), 14)

    def test_search_window_before_first_line(self):
        # Bounds less than 12 h behind the first line (23:17:29) are on its day.
        log = write_txt(self.dir)
        self.assertEqual(self.count(log, "--after", "23:00:00", "--before", "23:19:00"), 3)
        self.assertEqual(self.count(log, "--before", "23:00:00"), 0)
        self.assertEqual(self.count(log, "--after", "12:00:00"), 18)
        # A --before behind --after but within 12 h is on the same day, so the window is empty.
        self.assertEqual(self.count(log, "--after", "23:19:00", "--before", "23:18:00"), 0)

    def test_search_bad_arguments(self):
        log = write_txt(self.dir)
        for args in (("--pattern", "("), ("--after", "25:00:00")):
            res = run("viavi_log_search.py", str(log), *args)
            self.assertEqual(res.returncode, 2)
            self.assertIn("error: argument", res.stderr)
            self.assertNotIn("Traceback", res.stderr)


if __name__ == "__main__":
    unittest.main()
