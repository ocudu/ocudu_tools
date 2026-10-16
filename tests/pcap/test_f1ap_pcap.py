#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Smoke test for the F1AP pcap scripts against a captured single-UE F1AP trace.

Runs f1ap_messages.py and f1ap_ue_ids.py (skills/analyze-ran-log/scripts/pcap)
as subprocesses against f1ap_pcap_test.pcap -- a real F1Setup + RRC/NAS
attach + UEContextRelease capture for one UE -- and checks their --json
output against known-good values.

Usage:
    python3 f1ap_pcap_test.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
PCAP = TEST_DIR.parents[1] / "tools" / "parsers" / "tests" / "pcap" / "f1ap_pcap_test.pcap"
SCRIPTS_DIR = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "pcap"


def run_json(script: str, *args: str) -> list:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(PCAP), *args, "--json"],
        capture_output=True, text=True, check=True,
    )
    # A "cache: <path> (hit|miss)" status line precedes the JSON payload.
    return json.loads(proc.stdout[proc.stdout.find("["):])


@unittest.skipUnless(shutil.which("tshark"), "tshark not installed")
class F1apPcapTest(unittest.TestCase):
    def test_f1ap_messages(self):
        records = run_json("f1ap_messages.py")
        self.assertEqual(len(records), 32)
        self.assertEqual(records[0]["message"], "F1Setup")

        setup_req = next(r for r in records if r["rrc"] == "rrcSetupRequest")
        self.assertEqual(setup_req["message"], "InitialULRRCMessageTransfer")
        self.assertEqual(setup_req["crnti"], "17921")

    def test_f1ap_messages_rrc_filter(self):
        records = run_json("f1ap_messages.py", "--rrc")
        self.assertTrue(records)
        self.assertTrue(all(r["rrc"] for r in records))

    def test_f1ap_ue_ids(self):
        ues = run_json("f1ap_ue_ids.py")
        self.assertEqual(len(ues), 1)

        ue = ues[0]
        self.assertEqual(ue["frame"], 3)
        self.assertEqual(ue["message"], "InitialULRRCMessageTransfer")
        self.assertEqual(ue["cu_ue_f1ap_id"], "0")
        self.assertEqual(ue["du_ue_f1ap_ids"], ["0"])
        self.assertEqual(ue["crntis"], ["17921"])

    def test_f1ap_ue_ids_filter(self):
        self.assertEqual(len(run_json("f1ap_ue_ids.py", "--ue", "0")), 1)
        self.assertEqual(run_json("f1ap_ue_ids.py", "--ue", "does-not-exist"), [])
        # An F1AP id only, never a C-RNTI: 4601 is the hex form of the C-RNTI of the UE.
        self.assertEqual(run_json("f1ap_ue_ids.py", "--ue", "4601"), [])
        for rnti in ("4601", "0x4601", "17921"):
            self.assertEqual(len(run_json("f1ap_ue_ids.py", "--rnti", rnti)), 1, rnti)
        self.assertEqual(run_json("f1ap_ue_ids.py", "--rnti", "4602"), [])

    def test_f1ap_messages_filters(self):
        nof_ue_messages = len(run_json("f1ap_messages.py", "--ue", "0"))
        self.assertGreater(nof_ue_messages, 1)
        self.assertEqual(run_json("f1ap_messages.py", "--ue", "17921"), [])
        # All messages of the UE context, not only those that name the C-RNTI.
        records = run_json("f1ap_messages.py", "--rnti", "4601")
        self.assertEqual(records, run_json("f1ap_messages.py", "--ue", "0"))
        self.assertEqual(sum(r["crnti"] == "17921" for r in records), 1)
        self.assertEqual(run_json("f1ap_messages.py", "--rnti", "4602"), [])

    def test_invalid_rnti(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "f1ap_ue_ids.py"), str(PCAP), "--rnti", "zz"],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not an RNTI", proc.stderr)


if __name__ == "__main__":
    unittest.main()
