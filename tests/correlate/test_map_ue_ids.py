#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests of the argument check of map_ue_ids.py.

Usage:
    python3 test_map_ue_ids.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "analyze-ran-log" / "scripts" / "correlate" / "map_ue_ids.py"


class MapUeIdsTest(unittest.TestCase):
    def test_directory(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "f1ap.pcap").write_bytes(b"")
            res = subprocess.run([sys.executable, str(SCRIPT), d], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("is not a pcap file; its pcaps: f1ap.pcap", res.stderr)
        self.assertNotIn("Traceback", res.stderr)


if __name__ == "__main__":
    unittest.main()
