#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the input resolution of ue_log_summary.py and resolve.py, and of the counts and anomalies of ue_log_summary.py.

The fixture lines are real amari-ue log lines.

Usage:
    python3 test_ue_log_summary.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "analyze-ran-log" / "scripts" / "amari-ue"

UE_LOG = """\
# lteue version 2026-06-12, gcc 15.2.0, glibc 2.17, avx2, Linux x86_64 6.17.0-41-generic
# Started on 2026-09-29 21:04:13
21:04:13.636 [NAS] -  0001 New state : 5GMM-NULL   CM-IDLE
21:04:17.850 [MAC] -  0007 00 Contention resolution failed or timer expired
21:04:17.850 [RRC] -  0007  - Random access failure
21:04:18.001 [RRC] -  0001  - PDCP reestablish for SRB 1 without reconfigurationWithSync or after RRC Resume; ignore it
21:04:18.002 [PHY] DL 0001 00 4601    16.1 PDSCH: harq=0 prb=1:11 symb=2:12 CW0: tb_len=111 mod=2 rv_idx=0 cr=0.37 crc=FAIL snr=5.5 epre=-47.1
# Ended on 2026-09-29 21:04:51
"""

# A Retina run: log_options all.level=warn, time.us=1.
US_UE_LOG = """\
# lteue version 2026-06-12, gcc 15.2.0, glibc 2.17, avx2, Linux x86_64 6.11.9-100.fc39.x86_64
# Started on 2026-10-18 21:05:48 [1c10c4e3800a53da][05afc694]
21:05:48.244032 [S72] -  0 type=SLOT dir=UL n_f=80 n_sf=5 slot=0
21:05:50.310527 [RRC] -  0001  - Random access failure
21:05:50.310529 [MAC] -  0001 00 Contention resolution failed or timer expired
21:05:51.100000 [RRC] -  0001  - T300 expired
# Ended on 2026-10-18 21:06:12
"""

US_UE_CFG = """\
{
  log_options: "all.level=warn,s72.level=info,time.us=1,all.max_size=0,ip.level=error",
  ue_list: [
    {
      ue_count: 2,
      imsi: "001010123456789",
      sim_events: [
        { start_time: 0.0, event: "power_on" },
        { start_time: 0.1, end_time: 20, dst_addr: "192.168.3.1", payload_len: 1400,
          bit_rate:  100000000.0, event: "cbr_send" },
      ]
    },
  ],
}
"""

US_STDOUT = """\
[[::]:2001] CBR_SEND: sent 0, recv 0
"""


def stats(time: float, prach: int, setups: int, accepts: int, dl_err: int) -> dict:
    return {
        "message": "stats", "time": time,
        "cells": {"0": {"dl_bitrate": 10205005, "ul_bitrate": 0, "dl_rx_count": 100,
                        "dl_err_count": dl_err, "ul_tx_count": 50, "ul_retx_count": 1}},
        "counters": {"messages": {"prach": prach, "nr_rrc_setup_request": setups, "nr_rrc_setup": setups,
                                  "5gs_nas_registration_request": setups,
                                  "5gs_nas_registration_accept": accepts},
                     "errors": {}},
    }


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True)


class UeLogSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, rel: str, name: str = "ue.log") -> Path:
        d = self.root / rel
        d.mkdir(parents=True)
        (d / name).write_text(UE_LOG)
        return d / name

    def test_named_file_is_parsed(self):
        ue0 = self.make_run("cap", "ue0.log")
        res = run("ue_log_summary.py", str(ue0))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("UE log        : ue0.log", res.stdout)
        self.assertIn("5GMM-NULL CM-IDLE", res.stdout)
        res = run("resolve.py", str(ue0), "--json")
        self.assertEqual(json.loads(res.stdout)["present"], ["ue0.log"])

    def test_dir_without_ue_log_is_an_error(self):
        self.make_run("cap", "ue0.log")
        for script in ("ue_log_summary.py", "resolve.py"):
            res = run(script, str(self.root / "cap"))
            self.assertEqual(res.returncode, 1)
            self.assertIn("No ue.log found", res.stdout + res.stderr)
            self.assertIn("found ue0.log", res.stdout + res.stderr)

    def test_non_log_file_is_rejected(self):
        bad = self.root / "ue.log.html"
        bad.write_text("<html><body>x</body></html>\n")
        res = run("ue_log_summary.py", str(bad))
        self.assertEqual(res.returncode, 1)
        self.assertIn("does not look like an Amarisoft UE log", res.stderr)

    def test_error_lines_are_failure_records(self):
        res = run("ue_log_summary.py", str(self.make_run("run")))
        # The two failure records; neither the PHY crc=FAIL nor the "without reconfigurationWithSync" note.
        self.assertIn("Error log lines    : 2", res.stdout)
        self.assertIn("PHY CRC failures   : 1", res.stdout)
        self.assertIn("Handovers (sync)   : 0", res.stdout)

    def test_microsecond_records_metrics_and_quiet_log(self):
        d = self.root / "run"
        d.mkdir()
        (d / "ue.log").write_text(US_UE_LOG)
        (d / "amarisoft_ue.cfg").write_text(US_UE_CFG)
        (d / "stdout.log").write_text(US_STDOUT)
        (d / "metrics.json").write_text(json.dumps([stats(0.6, 3, 1, 0, 2), stats(5.7, 12, 1, 1, 3)]))
        res = run("ue_log_summary.py", str(d))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Error log lines    : 3", res.stdout)
        self.assertIn("NOTE: amarisoft_ue.cfg log_options all.level=warn (info/debug: s72)", res.stdout)
        self.assertIn("=== metrics.json (2 stats records, up to t=5.7 s) ===", res.stdout)
        self.assertRegex(res.stdout, r"prach +12\n")
        self.assertIn("Cell 0: DL 10.2, UL 0.0 Mbit/s (last interval); DL err 5/200 rx", res.stdout)
        self.assertIn("metrics.json: 12 PRACH for 1 RRC setups/reestablishments", res.stdout)
        self.assertIn("metrics.json: 1 registration accepts for 2 simulated UEs (1 requests)", res.stdout)
        self.assertIn("CBR_SEND [[::]:2001] sent 0 (cbr_send bit_rate 1e+08): suspected no UL traffic",
                      res.stdout)
        res = run("resolve.py", str(d / "ue.log"), "--json")
        self.assertTrue(json.loads(res.stdout)["ok"])

    def test_token_cbr_send_and_empty_metrics(self):
        d = self.root / "run"
        d.mkdir()
        (d / "ue.log").write_text(UE_LOG)
        (d / "amarisoft_ue.cfg").write_text(US_UE_CFG.replace("100000000.0", "1"))
        (d / "stdout.log").write_text(US_STDOUT)
        (d / "metrics.json").write_text("")
        res = run("ue_log_summary.py", str(d))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("CBR_SEND [[::]:2001] sent 0", res.stdout)
        self.assertNotIn("=== metrics.json", res.stdout)

    def test_components_are_distinct_paths(self):
        # Same component name under two test dirs, and amarisoft-ue-1 beside amarisoft-ue-10.
        self.make_run("test_gnb[a]/amarisoft-ue-1/2026-09-29_21-04-12")
        self.make_run("test_gnb[b]/amarisoft-ue-1/2026-09-29_21-04-12")
        self.make_run("test_gnb[b]/amarisoft-ue-10/2026-09-29_21-04-12")
        res = run("resolve.py", str(self.root), "--json")
        report = json.loads(res.stdout)
        self.assertEqual(report["components"], [
            "test_gnb[a]/amarisoft-ue-1", "test_gnb[b]/amarisoft-ue-1", "test_gnb[b]/amarisoft-ue-10"])
        self.assertEqual(report["resolved_component"], "test_gnb[b]/amarisoft-ue-10")
        res = run("resolve.py", str(self.root / "test_gnb[b]" / "amarisoft-ue-1"), "--json")
        self.assertNotIn("components", json.loads(res.stdout))


if __name__ == "__main__":
    unittest.main()
