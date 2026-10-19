#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the --rnti filter, the RNTIs shown, the logs picked and the PUCCH statuses of correlate_radio.py.

Usage:
    python3 test_correlate_radio.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "correlate_radio.py"
GNB_LOG = """\
2026-04-29T14:27:21.281595 [PHY     ] [I] [  123.14] PUSCH: rnti=0x460b tbs=11 crc=KO sinr=97.0dB
2026-04-29T14:27:21.291595 [PHY     ] [I] [  123.15] PUSCH: rnti=0x4601 tbs=11 crc=KO sinr=97.0dB
"""
UE_LOG = "14:27:21.273 [PHY] UL 0035 00 460b  123.14 PUSCH: harq=0 tb_len=11\n"
PUCCH_GNB = "2026-04-29T14:27:21.271595 [PHY     ] [I] [{slot:>8}] PUCCH: rnti=0x4601 format=1 prb1=4 {rest}\n"


class CorrelateRadioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "gnb.log").write_text(GNB_LOG)
        (self.dir / "ue.log").write_text(UE_LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, rnti: str | None, kind: str = "pusch") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), str(self.dir), "--gnb", str(self.dir / "gnb.log"), "--ue",
                               str(self.dir / "ue.log"), "--kind", kind] + (["--rnti", rnti] if rnti else []),
                              capture_output=True, text=True)

    def test_rnti_in_any_form(self):
        # 17931 is 0x460b in decimal, as F1AP pcaps hold it, not the hex 0x17931.
        for rnti in ("460b", "0x460B", "17931"):
            res = self.run_script(rnti)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("0x460b   rx-ko/ue-tx", res.stdout, rnti)
            self.assertNotIn("0x4601", res.stdout, rnti)

    def test_rnti_shown_as_logged(self):
        (self.dir / "gnb.log").write_text(GNB_LOG.replace("0x460b", "0x44"))
        (self.dir / "ue.log").write_text(UE_LOG.replace("460b", "0044"))
        res = self.run_script("44")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("0x44     rx-ko/ue-tx", res.stdout)

    def test_split_run_takes_the_du_log(self):
        for comp, name, text in (("ocudu-cu-1", "cu.log", "2026-04-29T14:27:20.000000 [CU-CP   ] [I] CU-CP started\n"),
                                 ("ocudu-du-1-1", "du.log", GNB_LOG), ("amarisoft-ue-1", "ue.log", UE_LOG)):
            run = self.dir / "test" / comp / "2026-04-29_14-27-00"
            run.mkdir(parents=True)
            (run / name).write_text(text)
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.dir / "test")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("du.log", res.stdout.splitlines()[0])
        self.assertIn("rx-ko/ue-tx", res.stdout)
        cu = self.dir / "test" / "ocudu-cu-1" / "2026-04-29_14-27-00" / "cu.log"
        res = subprocess.run([sys.executable, str(SCRIPT), "--gnb", str(cu), "--ue", str(self.dir / "ue.log")],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no PHY lines", res.stderr)

    def test_pucch_without_ue_tx(self):
        # SR and CSI occasions the UE did not use are not anomalies, unlike a false SR or a missed HARQ-ACK.
        (self.dir / "gnb.log").write_text(
            PUCCH_GNB.format(slot="10.4", rest="sr=no metric=-nan sinr=-infdB")
            + PUCCH_GNB.format(slot="11.4", rest="sr=no metric=0.2 sinr=-22.3dB")
            + PUCCH_GNB.format(slot="12.4", rest="csi1=2222 sinr=-17.6dB")
            + PUCCH_GNB.format(slot="13.4", rest="sr=yes metric=2.4 sinr=-20.6dB")
            + PUCCH_GNB.format(slot="14.4", rest="ack=11 csi1=2222 sinr=-20.6dB")
            # Two resources of one HARQ-ACK occasion.
            + PUCCH_GNB.format(slot="15.4", rest="ack=2 metric=0.3 sinr=-17.6dB")
            + PUCCH_GNB.format(slot="15.4", rest="ack=2 metric=-nan sinr=-infdB")
            + PUCCH_GNB.format(slot="16.4", rest="ack=22 csi1=2222 sinr=-17.6dB"))
        res = self.run_script("4601", kind="pucch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("4  no-detection", res.stdout)
        self.assertIn("2  ue-missed-dl", res.stdout)
        self.assertIn("13.4  0x4601   ue-missing-tx", res.stdout)
        self.assertIn("14.4  0x4601   ue-missing-tx", res.stdout)
        self.assertIn("15.4  0x4601   ue-missed-dl", res.stdout)
        self.assertIn("16.4  0x4601   ue-missed-dl", res.stdout)

    def test_msg4_ack_takes_the_msg3_rnti(self):
        # Two UEs ack their Msg4 at 221.9 with RNTI 0000; 0x4606 has a negative SR there.
        (self.dir / "gnb.log").write_text(
            PUCCH_GNB.format(slot="221.9", rest="sr=no metric=0.2 sinr=-32.7dB").replace("4601", "4606")
            + PUCCH_GNB.format(slot="221.9", rest="ack=1 metric=22529.3 sinr=31.5dB").replace("4601", "460c")
            + PUCCH_GNB.format(slot="221.9", rest="ack=1 metric=22389.2 sinr=31.5dB").replace("4601", "460d"))
        (self.dir / "ue.log").write_text(
            "14:27:21.250 [PHY] UL 0002 00 460d  220.14 PUSCH: harq=0 tb_len=11\n"
            "14:27:21.251 [PHY] UL 0007 00 460c  220.14 PUSCH: harq=0 tb_len=11\n"
            "14:27:21.268 [PHY] DL 0002 00 010b   249.7 PDCCH: ss_id=1 dci=1_0\n"
            "14:27:21.270 [PHY] UL 0007 00 0000   221.9 PUCCH: format=1 ack=1\n"
            "14:27:21.270 [PHY] UL 0002 00 0000   221.9 PUCCH: format=1 ack=1\n"
            # No earlier UL of UE id 0009, so its RNTI stays unknown.
            "14:27:21.270 [PHY] UL 0009 00 0000   221.9 PUCCH: format=1 ack=1\n")
        res = self.run_script("4606", kind="pucch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("1  no-detection", res.stdout)
        self.assertNotIn("rx-ok", res.stdout)
        for rnti in ("460c", "460d"):
            res = self.run_script(rnti, kind="pucch")
            self.assertIn("1  rx-ok", res.stdout, rnti)
            self.assertIn("No anomalies.", res.stdout, rnti)
        res = self.run_script(None, kind="pucch")
        self.assertIn("2  rx-ok", res.stdout)
        self.assertIn("1  no-detection", res.stdout)
        self.assertIn("221.9  0x0      gnb-missing", res.stdout)

    def test_pucch_of_unknown_rnti_and_extra_resource(self):
        # The UE logs RNTI 0000 for the Msg4 HARQ-ACK, with no earlier UL TX to take the RNTI of; the gNB decodes
        # two resources of 0x4602 at 35.4.
        (self.dir / "gnb.log").write_text(
            PUCCH_GNB.format(slot="34.9", rest="ack=1 metric=76153.9 sinr=36.8dB")
            + PUCCH_GNB.format(slot="35.4", rest="ack=2 metric=0.3 sinr=-17.6dB").replace("4601", "4602")
            + PUCCH_GNB.format(slot="35.4", rest="ack=1 metric=1477.9 sinr=17.4dB").replace("4601", "4602"))
        (self.dir / "ue.log").write_text(
            "14:27:21.270 [PHY] UL 0001 01 0000    34.9 PUCCH: format=1 prb=0 ack=1 p=-10\n"
            "14:27:21.272 [PHY] UL 0002 01 4602    35.4 PUCCH: format=1 prb=0 ack=1 p=-10\n")
        res = self.run_script(None, kind="pucch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("2  rx-ok", res.stdout)
        self.assertNotIn("ue-missing-tx", res.stdout)
        # The unknown RNTI may be another UE's, so the filter drops it.
        res = self.run_script("4601", kind="pucch")
        self.assertIn("1  ue-missing-tx", res.stdout)
        res = self.run_script("4602", kind="pucch")
        self.assertIn("1  rx-ok", res.stdout)
        self.assertIn("1  gnb-extra-rx", res.stdout)
        self.assertIn("No anomalies.", res.stdout)

    def test_one_sided(self):
        for flag, name, missing in (("--gnb", "gnb.log", "no UE log"), ("--ue", "ue.log", "no gNB or DU log")):
            # No positional path: the current dir is not a run and is not searched.
            res = subprocess.run([sys.executable, str(SCRIPT), flag, str(self.dir / name)], capture_output=True,
                                 text=True, cwd=self.dir)
            self.assertEqual(res.returncode, 1)
            self.assertIn(f"{missing} ({'--ue' if flag == '--gnb' else '--gnb'}); nothing to correlate", res.stderr)

    def test_invalid_rnti(self):
        res = self.run_script("zz")
        self.assertEqual(res.returncode, 2)
        self.assertIn("invalid RNTI", res.stderr)


if __name__ == "__main__":
    unittest.main()
