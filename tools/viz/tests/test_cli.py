# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from viz.cli import _open_runs, _Progress
from viz.registry import SourceRegistry
from viz.sources.log_metrics import LogMetricsSource
from viz.store import StoreCache

from .helpers import write_log
from .test_registry import wait_ready


class FailingSource(LogMetricsSource):
    """Logs that fail to parse, like a pcap of another protocol."""

    name = "failing"

    def parse(self, path, writer, progress=None):
        raise ValueError("unsupported pcap")


class ProgressTest(unittest.TestCase):
    def test_one_line_for_all_files(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            progress = _Progress(["gnb.log", "f1ap.pcap"])
            progress.reporter(1)(10, 10)
            progress.reporter(0)(5, 10)
            progress.finish()
        lines = [line.strip() for line in out.getvalue().split("\r") if line.strip()]
        self.assertEqual(lines, [
            "Parsing 2 file(s): 1 done, gnb.log 0%",
            "Parsing 2 file(s): 1 done, gnb.log 50%",
            "Parsing 2 file(s): 2 done",
        ])
        self.assertTrue(out.getvalue().endswith("\n"))


class OpenRunsTest(unittest.TestCase):
    def test_file_failing_to_parse_is_added_in_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp).resolve()
            log, bad = write_log(d / "gnb.log"), write_log(d / "bad.log")
            cache = StoreCache(d / "cache")
            registry = SourceRegistry(cache, [LogMetricsSource()], [d])
            with contextlib.redirect_stderr(io.StringIO()) as err:
                _open_runs(registry, cache, [(d, True, [(log, LogMetricsSource()), (bad, FailingSource())])])
            # Events parse in the background, and must finish before the cache is removed.
            entries = [wait_ready(registry, i) for i in range(2)]
            self.assertEqual([e.status for e in entries], ["ready", "error"])
            self.assertEqual(entries[1].error, "bad.log: unsupported pcap")
            self.assertEqual(registry.runs()[0].source_ids, [0, 1])
            self.assertIn("unsupported pcap", err.getvalue())


if __name__ == "__main__":
    unittest.main()
