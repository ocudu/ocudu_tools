# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from viz.sources import log_metrics
from viz.sources.log_metrics import LogMetricsSource
from viz import store as store_module
from viz.store import QueryError, StoreCache

from .helpers import _ts, write_log


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = write_log(self.dir / "gnb.log")
        self.cache = StoreCache(self.dir / "cache")
        self.store = self.cache.open(self.log, LogMetricsSource())

    def tearDown(self):
        self.tmp.cleanup()

    def test_datasets_metadata(self):
        self.assertEqual(sorted(self.store.datasets), ["mac", "sched_ue"])
        ue = self.store.datasets["sched_ue"]
        self.assertEqual(ue["fields"]["dl_brate"], "number")
        self.assertEqual(ue["fields"]["rnti"], "text")
        self.assertEqual(ue["units"]["dl_brate"], "bps")
        self.assertEqual(ue["units"]["dl_bs"], "bytes")
        self.assertEqual(ue["context"], ["ue", "pci", "rnti"])
        # Metrics span 9 s, inside the 13 s of the log.
        self.assertEqual(ue["t_max"] - ue["t_min"], 9)
        self.assertEqual(ue["t_min"] - self.store.meta["t_min"], 2)
        # The log spans 2 s before the first and 2 s after the last metrics line.
        self.assertEqual(self.store.meta["t_max"] - self.store.meta["t_min"], 13)

    def test_full_resolution_series(self):
        res = self.store.series("sched_ue", "dl_brate", split_by="ue")
        self.assertFalse(res["downsampled"])
        self.assertEqual(res["unit"], "bps")
        self.assertEqual([s["label"] for s in res["series"]], ["ue=0", "ue=1"])
        s = res["series"][0]
        self.assertEqual(s["v"], [1000 * i for i in range(10)])
        self.assertEqual(len(s["t"]), 10)
        # Line 1 is the boot line, then 4 lines per second for 2 UEs.
        self.assertEqual(s["record"][:2], [4, 8])

    def test_time_window_and_split_values(self):
        t_min = self.store.meta["t_min"]
        res = self.store.series("sched_ue", "pusch_snr_db", t0=t_min + 4, t1=t_min + 6, split_by="ue", split_values=["1"])
        self.assertEqual([s["label"] for s in res["series"]], ["ue=1"])
        self.assertEqual(res["series"][0]["v"], [21.5, 21.5, 21.5])

    def test_split_cap_without_explicit_values(self):
        log = write_log(self.dir / "many.log", nof_seconds=2, nof_ues=30)
        store = self.cache.open(log, LogMetricsSource())
        res = store.series("sched_ue", "dl_brate", split_by="ue", max_splits=5)
        self.assertEqual(res["total_splits"], 30)
        self.assertEqual([s["split"] for s in res["series"]], [0, 1, 2, 3, 4])
        res = store.series("sched_ue", "dl_brate", split_by="ue", split_values=["7", "29"], max_splits=5)
        self.assertEqual([s["split"] for s in res["series"]], [7, 29])

    def test_downsampled_series_keeps_extremes(self):
        res = self.store.series("mac", "wall_clock_latency_max", width=2, max_points=5)
        self.assertTrue(res["downsampled"])
        v = res["series"][0]["v"]
        self.assertIn(0, v)
        self.assertIn(90, v)
        self.assertLessEqual(len(v), 4)

    def test_filtered_series(self):
        res = self.store.series("sched_ue", "dl_brate", split_by="ue", filter_expr="rnti == 0x4601 and dl_brate >= 5000")
        self.assertEqual([s["label"] for s in res["series"]], ["ue=1"])
        self.assertEqual(res["series"][0]["v"], [5000, 6000, 7000, 8000, 9000])
        self.assertEqual(res["total_splits"], 1)

    def test_invalid_filter(self):
        with self.assertRaisesRegex(QueryError, "Filter: Unknown field 'nope'"):
            self.store.series("sched_ue", "dl_brate", filter_expr="nope > 1")

    def test_stats(self):
        res = self.store.stats("sched_ue", "dl_brate", split_by="ue")
        self.assertEqual(res["unit"], "bps")
        self.assertFalse(res["sampled"])
        s = res["series"][1]
        self.assertEqual((s["label"], s["count"], s["min"], s["max"]), ("ue=1", 10, 0, 9000))
        self.assertEqual(s["mean"], 4500)
        self.assertEqual(s["p50"], 4500)
        self.assertAlmostEqual(s["p95"], 8550)
        self.assertAlmostEqual(s["p99"], 8910)

    def test_stats_window_and_filter(self):
        t_min = self.store.meta["t_min"]
        res = self.store.stats("sched_ue", "dl_brate", t0=t_min + 4, t1=t_min + 6, filter_expr="ue == 0")
        self.assertEqual(len(res["series"]), 1)
        self.assertEqual((res["series"][0]["count"], res["series"][0]["min"], res["series"][0]["max"]), (3, 2000, 4000))

    def test_stats_sampled_percentiles(self):
        res = self.store.stats("sched_ue", "dl_brate", max_exact_points=5)
        self.assertTrue(res["sampled"])
        s = res["series"][0]
        self.assertEqual(s["count"], 20)
        self.assertIsNotNone(s["p50"])

    def test_stats_empty_window(self):
        res = self.store.stats("sched_ue", "dl_brate", filter_expr="dl_brate > 1e9")
        self.assertEqual(res["series"], [])

    def test_histogram_continuous(self):
        res = self.store.histogram("sched_ue", "dl_brate", split_by="ue", bins=3)
        self.assertEqual(res["edges"], [0, 3000, 6000, 9000])
        self.assertEqual([s["label"] for s in res["series"]], ["ue=0", "ue=1"])
        # 0..9000 in steps of 1000: the maximum falls into the last bin.
        self.assertEqual(res["series"][0]["counts"], [3, 3, 4])

    def test_histogram_integer_bins(self):
        res = self.store.histogram("mac", "wall_clock_latency_avg")
        self.assertEqual(res["edges"][:2], [-0.5, 0.5])
        self.assertEqual(len(res["edges"]), 11)
        self.assertEqual(res["series"][0]["counts"], [1] * 10)

    def test_histogram_constant_and_empty(self):
        res = self.store.histogram("sched_ue", "max_crc_delay")
        self.assertEqual(res["series"][0]["counts"], [20])
        self.assertEqual(len(res["edges"]), 2)
        res = self.store.histogram("sched_ue", "dl_brate", filter_expr="dl_brate < 0")
        self.assertEqual((res["edges"], res["series"]), ([], []))

    def test_instance_selection(self):
        log = write_log(self.dir / "exec.log", executors=True)
        store = self.cache.open(log, LogMetricsSource())
        ds = store.datasets["exec"]
        self.assertEqual((ds["label"], ds["instance"]), ("executors", "executor"))
        self.assertEqual(store.context_values("exec", "executor"), ["cell_exec", "du_ctrl_exec"])
        res = store.series("exec", "nof_executes", instance="du_ctrl_exec")
        self.assertEqual(res["series"][0]["v"], [200 + s for s in range(10)])
        self.assertEqual(store.stats("exec", "task_avg", instance="cell_exec")["series"][0]["mean"], 10)
        self.assertEqual(store.histogram("exec", "task_avg", instance="cell_exec")["series"][0]["counts"], [10])
        both = store.series("exec", "task_avg", split_by="executor")
        self.assertEqual([s["label"] for s in both["series"]], ["executor=cell_exec", "executor=du_ctrl_exec"])

    def test_instance_on_dataset_without_instance(self):
        with self.assertRaisesRegex(QueryError, "no instance field"):
            self.store.series("mac", "nof_slots", instance="x")
        self.assertEqual((self.store.datasets["mac"]["label"], self.store.datasets["mac"]["instance"]), ("mac", None))

    def test_table_rows(self):
        columns = self.store.table_columns("sched_ue")
        self.assertEqual(columns[:3], ["ue", "pci", "rnti"])
        self.assertIn("dl_brate", columns)
        rows = list(self.store.table_rows("sched_ue", limit=None))
        self.assertEqual(len(rows), 20)
        t, rec, *values = rows[2]
        self.assertEqual(rec, 8)
        self.assertEqual(dict(zip(columns, values))["dl_brate"], 1000)
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))

    def test_table_rows_selection(self):
        rows = list(self.store.table_rows("sched_ue", filter_expr="ue == 1", fields=["rnti", "dl_brate"], limit=3))
        self.assertEqual([r[2:] for r in rows], [("0x4601", 0), ("0x4601", 1000), ("0x4601", 2000)])
        self.assertEqual(self.store.count_table_rows("sched_ue", filter_expr="ue == 1"), 10)
        with self.assertRaisesRegex(QueryError, "Unknown fields"):
            self.store.table_columns("sched_ue", ["nope"])

    def test_table_rows_instance(self):
        log = write_log(self.dir / "exec.log", executors=True)
        store = self.cache.open(log, LogMetricsSource())
        self.assertEqual(store.count_table_rows("exec", instance="du_ctrl_exec"), 10)

    def open_with_events(self, cache=None):
        cache = cache or self.cache
        store = cache.open(write_log(self.dir / "events.log", events=True), LogMetricsSource())
        cache.open_events(store, LogMetricsSource())
        return store

    def test_events(self):
        store = self.open_with_events()
        self.assertEqual(store.event_counts, {"ra": 2, "lifecycle": 2, "failure": 1, "warning": 1})
        res = store.events()
        self.assertEqual([e["type"] for e in res["events"]], ["prach", "prach", "ue_create", "ue_create", "rlf", "warning"])
        prach = res["events"][1]
        self.assertEqual((prach["rnti"], prach["level"]), ("0x4601", "D"))
        self.assertIn("Processed slot events", store.records(around=prach["record"], count=1)[0]["text"])
        rlf = res["events"][4]
        self.assertEqual((rlf["category"], rlf["layer"], rlf["ue"], rlf["cause"]), ("failure", "DU-MNG", 1, "MAC"))
        self.assertIn("RLF detected", store.records(around=rlf["record"], count=1)[0]["text"])

    def test_search_across_blocks(self):
        log = write_log(self.dir / "long.log", nof_seconds=400, nof_ues=10)
        store = self.cache.open(log, LogMetricsSource())
        want = [i for i, line in enumerate(log.read_text().splitlines(), start=1) if "ue=7 " in line]
        # Blocks of one record offset each, so that matches span many blocks.
        with mock.patch.object(store_module, "_SEARCH_BLOCK_SIZE", 1):
            found, after = [], 0
            for _ in want:
                after = store.search("ue=7 ", after=after)["record"]
                found.append(after)
            self.assertEqual(found, want)
            self.assertEqual(store.search("ue=7 ", after=want[-1]), {"record": want[0], "wrapped": True})
            back, after = [], want[-1] + 1
            for _ in want:
                after = store.search("ue=7 ", after=after, backward=True)["record"]
                back.append(after)
            self.assertEqual(back, want[::-1])

    def test_trace_failed_creation(self):
        lines = [
            f'{_ts(1)} [DU-MNG  ] [I] ue=0 rnti=0x4601 proc="UE Create": Procedure started...',
            f'{_ts(2)} [DU-MNG  ] [I] ue=1 rnti=0x4601 proc="UE Create": Procedure started...',
            f'{_ts(2.1)} [DU-MNG  ] [W] ue=1 rnti=0x4601 proc="UE Create": Procedure failed. Cause: C-RNTI clash.',
        ]
        path = self.dir / "failed.log"
        path.write_text("\n".join(lines) + "\n")
        store = self.cache.open(path, LogMetricsSource())
        self.cache.open_events(store, LogMetricsSource())
        lanes = store.trace()["lanes"]
        self.assertEqual([(lane["ue"], lane["open"]) for lane in lanes], [(0, True), (1, False)])

    def test_repeated_warnings(self):
        late = "[OFH     ] [W] Real-time timing worker woke up late, skipped '{}' symbols."
        lines = [f"{_ts(1 + 0.2 * i)} {late.format(i % 3)}" for i in range(5)]
        # The same warning after a gap, a warning of another message, and one of a UE.
        lines += [f"{_ts(4)} {late.format(9)}", f"{_ts(4.1)} [OFH     ] [W] Other warning", f"{_ts(4.2)} [MAC     ] [W] ue=3: Late HARQ 5"]
        lines += [f"{_ts(4.3)} [MAC     ] [W] ue=3: Late HARQ 6", f"{_ts(4.4)} [MAC     ] [W] ue=4: Late HARQ 7"]
        path = self.dir / "repeated.log"
        path.write_text("\n".join(lines) + "\n")
        store = self.cache.open(path, LogMetricsSource())
        self.cache.open_events(store, LogMetricsSource())
        evs = store.events()["events"]
        self.assertEqual([(e["record"], e["count"], e["ue"]) for e in evs], [(1, 5, None), (6, None, None), (7, None, None), (8, 2, 3), (10, None, 4)])
        self.assertAlmostEqual(evs[0]["span"], 0.8)
        # Counts are of the lines.
        self.assertEqual(store.event_counts, {"warning": 10})

    def test_events_selection(self):
        store = self.open_with_events()
        t_min = store.meta["t_min"]
        self.assertEqual([e["type"] for e in store.events(categories=["failure", "warning"])["events"]], ["rlf", "warning"])
        self.assertEqual(store.event_type_counts, {"prach": 2, "ue_create": 2, "rlf": 1, "warning": 1})
        by_type = store.events(categories=["warning"], types=["prach", "rlf"])["events"]
        self.assertEqual([e["type"] for e in by_type], ["prach", "prach", "rlf", "warning"])
        self.assertEqual([e["type"] for e in store.events(categories=[], types=["rlf"])["events"]], ["rlf"])
        self.assertEqual([e["type"] for e in store.events(ue=1)["events"]], ["ue_create", "rlf"])
        self.assertEqual([e["type"] for e in store.events(t0=t_min + 6, t1=t_min + 11)["events"]], ["warning"])
        limited = store.events(limit=1)
        self.assertEqual((limited["total"], limited["truncated"], len(limited["events"])), (6, True, 1))
        self.assertEqual(store.events(categories=[])["events"], [])

    def test_no_events(self):
        self.cache.open_events(self.store, LogMetricsSource())
        self.assertTrue(self.store.events_ready)
        self.assertEqual((self.store.event_counts, self.store.events()["total"]), ({}, 0))

    def test_trace(self):
        store = self.open_with_events()
        res = store.trace()
        lanes = res["lanes"]
        self.assertEqual([(lane["ue"], lane["rnti"], lane["open"]) for lane in lanes], [(0, "0x4600", True), (1, "0x4601", True)])
        by_type = {}
        for e in res["events"]:
            by_type.setdefault(e["type"], []).append(e["lane"])
        ue1 = lanes[1]["lane"]
        self.assertEqual(by_type["prach"], [lanes[0]["lane"], ue1])
        self.assertEqual((by_type["rlf"], by_type["warning"]), ([ue1], [None]))
        # Open lanes stay active after their last event, which is before the window.
        t_min = store.meta["t_min"]
        late = store.trace(t0=t_min + 6, t1=t_min + 11)
        self.assertEqual((late["total_lanes"], [e["type"] for e in late["events"]]), (2, ["warning"]))
        self.assertEqual(store.trace(max_lanes=1)["total_lanes"], 2)

    def test_notes(self):
        self.assertEqual(self.store.meta["notes"], [])
        quiet = self.cache.open(write_log(self.dir / "quiet.log", log_level="warning"), LogMetricsSource())
        self.assertEqual(len(quiet.meta["notes"]), 1)
        self.assertIn("mac, du, rrc, cu, ngap at warning", quiet.meta["notes"][0])
        # RLF is a warning of the DU manager, missing only below that.
        self.assertNotIn("RLF", quiet.meta["notes"][0])
        silent = self.cache.open(write_log(self.dir / "silent.log", log_level="error"), LogMetricsSource())
        self.assertIn("RLF", silent.meta["notes"][0])
        verbose = self.cache.open(write_log(self.dir / "verbose.log", log_level="info"), LogMetricsSource())
        self.assertEqual(verbose.meta["notes"], [])

    def test_events_after_datasets(self):
        store = self.cache.open(write_log(self.dir / "events.log", events=True), LogMetricsSource())
        self.assertFalse(store.events_ready)
        self.assertEqual((store.event_counts, store.events()["total"]), ({}, 0))
        self.cache.open_events(store, LogMetricsSource())
        self.assertEqual((store.events_ready, store.events()["total"]), (True, 6))

    def test_events_cache(self):
        self.open_with_events()
        cached = sorted(p.name for p in (self.dir / "cache").iterdir())
        self.assertEqual(len(cached), 3)
        self.assertEqual(sum(name.endswith(".events.sqlite") for name in cached), 1)
        events_db = next((self.dir / "cache").glob("*.events.sqlite"))
        mtime = events_db.stat().st_mtime_ns
        self.assertEqual(self.open_with_events().events()["total"], 6)
        self.assertGreaterEqual(events_db.stat().st_mtime_ns, mtime)
        self.assertEqual(len(list((self.dir / "cache").glob("*.tmp"))), 0)

    def test_events_without_cache(self):
        store = self.open_with_events(StoreCache(enabled=False))
        self.assertEqual(store.events()["total"], 6)

    def test_invalid_queries(self):
        with self.assertRaises(QueryError):
            self.store.series("foo", "x")
        with self.assertRaises(QueryError):
            self.store.series("sched_ue", "rnti")
        with self.assertRaises(QueryError):
            self.store.series("sched_ue", 'dl_brate" OR 1=1 --')
        with self.assertRaises(QueryError):
            self.store.series("sched_ue", "dl_brate", split_by="nope")

    def test_context_values(self):
        self.assertEqual(self.store.context_values("sched_ue", "rnti"), ["0x4600", "0x4601"])

    def test_records_around(self):
        recs = self.store.records(around=4, count=3)
        self.assertEqual([r["record"] for r in recs], [3, 4, 5])
        self.assertIn("Scheduler UE ue=0", recs[1]["text"])

    def test_records_beyond_offset_interval(self):
        log = write_log(self.dir / "big.log", nof_seconds=60, nof_ues=20)
        store = self.cache.open(log, LogMetricsSource())
        recs = store.records(around=1200, count=1)
        self.assertEqual(recs[0]["record"], 1200)
        self.assertEqual(recs[0]["text"], log.read_text().splitlines()[1199])


class StoreCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = write_log(self.dir / "gnb.log")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reuses_cache(self):
        cache = StoreCache(self.dir / "cache")
        cache.open(self.log, LogMetricsSource())
        calls = []
        cache.open(self.log, LogMetricsSource(), progress=lambda done, total: calls.append(done))
        self.assertEqual(calls, [])
        self.assertEqual(len(list((self.dir / "cache").glob("*.sqlite"))), 1)

    def test_rebuilds_when_source_changes(self):
        cache = StoreCache(self.dir / "cache")
        cache.open(self.log, LogMetricsSource())
        write_log(self.log, nof_seconds=12)
        store = cache.open(self.log, LogMetricsSource())
        self.assertEqual(store.meta["t_max"] - store.meta["t_min"], 15)

    def test_overlapping_builds(self):
        # Two servers, e.g. in separate containers, building the same cache file at once.
        cache, other = StoreCache(self.dir / "cache"), StoreCache(self.dir / "cache")
        nested = []

        def progress(done, total):
            if not nested:
                nested.append(other.open(self.log, LogMetricsSource()))

        store = cache.open(self.log, LogMetricsSource(), progress=progress)
        self.assertEqual(len(nested), 1)
        self.assertEqual(len(store.series("mac", "nof_slots")["series"][0]["v"]), 10)
        self.assertEqual(len(list((self.dir / "cache").glob("*.sqlite"))), 1)
        self.assertEqual(list((self.dir / "cache").glob("*.tmp")), [])

    def test_cache_dir_is_private(self):
        cache = StoreCache(self.dir / "cache")
        cache.open(self.log, LogMetricsSource())
        self.assertEqual((self.dir / "cache").stat().st_mode & 0o777, 0o700)

    def test_evicts_least_recently_used(self):
        cache = StoreCache(self.dir / "cache", max_bytes=1)
        other = write_log(self.dir / "other.log")
        cache.open(self.log, LogMetricsSource())
        cache.open(other, LogMetricsSource())
        self.assertEqual(len(list((self.dir / "cache").glob("*.sqlite"))), 1)

    def test_clear(self):
        cache = StoreCache(self.dir / "cache")
        cache.open(self.log, LogMetricsSource())
        cache.clear()
        self.assertEqual(list((self.dir / "cache").glob("*.sqlite")), [])

    def test_in_memory(self):
        cache = StoreCache(self.dir / "cache", enabled=False)
        store = cache.open(self.log, LogMetricsSource())
        self.assertEqual(len(store.series("mac", "nof_slots")["series"][0]["v"]), 10)
        self.assertFalse((self.dir / "cache").exists())


class MetricsChunkTest(unittest.TestCase):
    def test_unit_warnings_are_returned(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gnb.log"
            path.write_text("2026-06-29T14:10:11.000000 [METRICS ] Scheduler cell pci=1 metrics: foo=3dB\n")
            with self.assertNoLogs("parsers", "WARNING"):
                res = log_metrics._parse_metrics_chunk(str(path), 0, path.stat().st_size, 1)
        self.assertEqual(list(res["warnings"]), [("sched", "foo")])


if __name__ == "__main__":
    unittest.main()
