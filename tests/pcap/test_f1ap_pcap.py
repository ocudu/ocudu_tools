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

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
PCAP = TEST_DIR.parents[1] / "tools" / "parsers" / "tests" / "pcap" / "f1ap_pcap_test.pcap"
# The UEs 0x4604 and 0x4605 of a run of reestablishments, 0x4604 reestablished five times under other C-RNTIs.
REEST_PCAP = PCAP.with_name("f1ap_reestablishment_test.pcap")
SCRIPTS_DIR = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "pcap"


def run_json(script: str, *args: str, pcap: Path = PCAP) -> list:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(pcap), *args, "--json"],
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

    def test_f1ap_messages_rnti_across_reestablishments(self):
        records = run_json("f1ap_messages.py", "--rnti", "4604", pcap=REEST_PCAP)
        self.assertEqual((records[0]["rrc"], records[0]["crnti"]), ("rrcSetupRequest", "17924"))
        self.assertEqual([r["crnti"] for r in records if r["rrc"] == "rrcReestablishmentRequest"],
                         ["17935", "17962", "18000", "18023", "18034"])
        self.assertEqual(records[-1]["message"], "UEContextRelease")
        self.assertNotIn("4", {r["du_ue_f1ap_id"] for r in records})
        # From a reestablishment on, without the contexts it continues.
        later = run_json("f1ap_messages.py", "--rnti", "460f", pcap=REEST_PCAP)
        self.assertEqual(later, records[records.index(later[0]):])
        self.assertEqual(later[0]["crnti"], "17935")
        both = run_json("f1ap_messages.py", "--ue", "13", "--rnti", "4604", pcap=REEST_PCAP)
        self.assertEqual(both, [r for r in records if "13" in (r["cu_ue_f1ap_id"], r["du_ue_f1ap_id"])])
        self.assertEqual(len(both), 7)

    def test_invalid_rnti(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS_DIR / "f1ap_ue_ids.py"), str(PCAP), "--rnti", "zz"],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not an RNTI", proc.stderr)


class F1apMessagesFiltersTest(unittest.TestCase):
    """--ue and --rnti of f1ap_messages.py on given messages, without tshark."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS_DIR))
        spec = importlib.util.spec_from_file_location("f1ap_messages", SCRIPTS_DIR / "f1ap_messages.py")
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    def test_ue_and_rnti_of_other_cu_and_du_ids(self):
        # Only the InitialULRRCMessageTransfer names the C-RNTI, and it has no gNB-CU UE F1AP ID.
        msgs = [
            {"frame": 1, "epoch": 1.0, "code": "11", "outcome": "initiating", "du_ue_f1ap_id": "7", "cu_ue_f1ap_id": None,
             "crnti": "17921", "links": {}},
            {"frame": 2, "epoch": 2.0, "code": "12", "outcome": "initiating", "du_ue_f1ap_id": "7", "cu_ue_f1ap_id": "3",
             "crnti": None, "links": {}},
            {"frame": 3, "epoch": 3.0, "code": "6", "outcome": "successful", "du_ue_f1ap_id": "7", "cu_ue_f1ap_id": "3",
             "crnti": None, "links": {}},
        ]
        records = [{"frame": m["frame"], "epoch": m["epoch"], "first_iso": "", "message": "", "rrc": None, "nas": None,
                    "cu_ue_f1ap_id": m["cu_ue_f1ap_id"], "du_ue_f1ap_id": m["du_ue_f1ap_id"], "crnti": m["crnti"]}
                   for m in msgs]
        cap = self.script.capture.Capture("f1ap", "", messages=msgs)
        out = io.StringIO()
        with tempfile.NamedTemporaryFile(suffix=".pcap") as pcap, \
                mock.patch.object(self.script.f1ap, "messages", return_value=records), \
                mock.patch.object(self.script.capture, "read", return_value=cap), contextlib.redirect_stdout(out):
            self.assertEqual(self.script.main([pcap.name, "--ue", "3", "--rnti", "4601", "--json"]), 0)
        self.assertEqual([r["frame"] for r in json.loads(out.getvalue())], [2, 3])


if __name__ == "__main__":
    unittest.main()
