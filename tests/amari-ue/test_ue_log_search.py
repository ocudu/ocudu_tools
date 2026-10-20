#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for ue_log_search.py against literal Amarisoft UE log examples.

The fixture below is assembled from real amari-ue log lines (single-UE
two-step RACH run: power-on, MIB/SIB1 broadcast, registration, RRC setup
request with its ASN.1 body, then deregistration) so the block-splitting,
per-layer, per-UE and timestamp-window filters are all exercised against
genuine log formatting rather than hand-written approximations.

Usage:
    python3 test_ue_log_search.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "amari-ue" / "ue_log_search.py"

SAMPLE_UE_LOG = """\
# lteue version 2026-05-13, gcc 15.2.0, glibc 2.17, avx2, Linux x86_64 6.17.0-41-generic
# Log file format:
# time layer dir ue_id {cell_id rnti sfn channel:} message
15:45:02.270 [NAS] -  0001 New state : 5GMM-NULL   CM-IDLE
15:45:02.309 [PROD] -  - SIM-Event: power_on
15:45:02.385 [RRC] DL f000 00 BCCH-BCH-NR: MIB
        {
          message mib: {
            systemFrameNumber '000000'B,
            subCarrierSpacingCommon scs30or120,
            ssb-SubcarrierOffset 8,
            dmrs-TypeA-Position pos2,
            pdcch-ConfigSIB1 {
              controlResourceSetZero 11,
              searchSpaceZero 0
            },
            cellBarred notBarred,
            intraFreqReselection allowed,
            spare '0'B
          }
        }

15:45:02.439 [PROD] -  - SIM-Event: cbr_recv
15:45:02.464 [PHY] DL f000 00 ffff    16.1 PDSCH: harq=si prb=1:11 symb=2:12 CW0: tb_len=111 mod=2 rv_idx=0 cr=0.37 crc=OK snr=54.5 epre=-47.1
15:45:02.702 [RRC] DL 0001 00 BCCH-NR: SIB1
15:45:02.702 [NAS] UL 0001 5GMM: Registration request
15:45:02.702 [NAS] -  0001 New state : 5GMM-REGISTERED-INITIATED   CM-IDLE
15:45:02.702 [RRC] UL 0001 00 CCCH-NR: RRC setup request
        {
          message c1: rrcSetupRequest: {
            rrcSetupRequest {
              ue-Identity randomValue: '111000010111111000100001000100100000000'B,
              establishmentCause mo-Signalling,
              spare '0'B
            }
          }
        }

15:45:02.709 [PHY] UL 0001 00    -   32.19 PRACH: sequence_index=61 prb=11:12 symb=0:12 two_steps=1 epre=0.0
15:45:02.709 [MAC] UL 0001 00 LCID:52 len=6 PAD:len=3
15:45:07.672 [PROD] -  - SIM-Event: deregister
15:45:07.672 [NAS] -  0001 New state : 5GMM-DEREGISTERED   CM-IDLE
15:45:07.672 [NAS] -  0001 local deregistration
15:45:09.399 [PROD] -  - SIM-Event: power_off
15:45:09.399 [NAS] -  0001 wrong state to deregister
15:45:09.401 [NAS] -  0001 New state : 5GMM-DEREGISTERED   CM-IDLE
15:45:09.401 [NAS] -  0001 New state : 5GMM-NULL   CM-IDLE
"""


class UeLogSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        (Path(cls.tmpdir.name) / "ue.log").write_text(SAMPLE_UE_LOG)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def run_search(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), self.tmpdir.name, *args],
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def run_count(self, *args: str) -> int:
        return int(self.run_search(*args, "--count").strip())

    def test_total_blocks(self):
        self.assertEqual(self.run_count(), 18)

    def test_layer_filter(self):
        self.assertEqual(self.run_count("--layer", "NAS"), 8)
        self.assertEqual(self.run_count("--layer", "nas"), 8)  # case-insensitive

    def test_nas_state_transitions(self):
        # Doc example: "All NAS state transitions".
        self.assertEqual(self.run_count("--layer", "NAS", "--pattern", "New state"), 5)

    def test_ue_filter(self):
        self.assertEqual(self.run_count("--ue", "0001"), 12)
        self.assertEqual(self.run_count("--ue", "f000"), 2)  # broadcast pseudo-id

    def test_timestamp_window(self):
        self.assertEqual(
            self.run_count("--after", "15:45:02.700", "--before", "15:45:02.710"), 6
        )

    def test_pattern_matches_multiline_block_body(self):
        # "rrcSetupRequest" only appears inside the ASN.1 body, not the header line.
        self.assertEqual(self.run_count("--pattern", "rrcSetupRequest"), 1)
        output = self.run_search("--pattern", "ue-Identity randomValue")
        self.assertIn("15:45:02.702 [RRC] UL 0001 00 CCCH-NR: RRC setup request", output)
        self.assertIn("ue-Identity randomValue:", output)

    def test_timestamp_window_across_midnight(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "ue.log"
            log.write_text(
                "23:59:59.000 [NAS] -  0001 New state : 5GMM-REGISTERED   CM-CONNECTED\n"
                "23:59:59.900 [PROD] -  - SIM-Event: deregister\n"
                "00:00:00.100 [NAS] -  0001 New state : 5GMM-DEREGISTERED   CM-IDLE\n"
                "00:00:01.000 [PROD] -  - SIM-Event: power_off\n"
            )

            def count(*args: str) -> int:
                proc = subprocess.run([sys.executable, str(SCRIPT), str(log), *args, "--count"],
                                      capture_output=True, text=True, check=True)
                return int(proc.stdout)

            self.assertEqual(count("--after", "23:59:59.500"), 3)
            self.assertEqual(count("--after", "23:59:59.500", "--before", "00:00:00.500"), 2)
            self.assertEqual(count("--before", "00:00:00.100"), 3)

    def test_bad_arguments_exit_2(self):
        for args in (("--pattern", "("), ("--after", "1:2")):
            proc = subprocess.run([sys.executable, str(SCRIPT), self.tmpdir.name, *args],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error: argument", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
