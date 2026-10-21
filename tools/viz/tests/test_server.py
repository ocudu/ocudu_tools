# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import importlib.util
import tempfile
import unittest
from pathlib import Path

from .helpers import write_log


class DisplayNamesTest(unittest.TestCase):
    def test_unique_names_are_kept(self):
        from viz.server import display_names

        self.assertEqual(display_names([Path("/a/gnb.log"), Path("/b/ue.log")]), ["gnb.log", "ue.log"])

    def test_shared_names_get_first_differing_dir(self):
        from viz.server import display_names

        paths = [Path("/r/du-1/2026-07-30_20-53-05/du.log"), Path("/r/du-2/2026-07-30_20-53-09/du.log"), Path("/r/cu/cu.log")]
        self.assertEqual(display_names(paths), ["du-1/\u2026/du.log", "du-2/\u2026/du.log", "cu.log"])
        self.assertEqual(display_names([Path("/r/a/x.log"), Path("/r/b/x.log")]), ["a/x.log", "b/x.log"])


@unittest.skipUnless(importlib.util.find_spec("httpx"), "httpx not installed")
class ServerTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from viz.server import create_app
        from viz.sources.log_metrics import LogMetricsSource
        from viz.store import StoreCache

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        cache = StoreCache(tmp / "cache")
        self.store = store = cache.open(write_log(tmp / "gnb.log", executors=True, events=True), LogMetricsSource())
        cache.open_events(store, LogMetricsSource())
        # A stand-in for the built frontend, so that the tests do not need Node.
        static = tmp / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<!doctype html><title>ocudu-viz</title>")
        (static / "assets" / "app.js").write_text("console.log('app');")
        self.client = TestClient(create_app([store], static))
        self.not_built_client = TestClient(create_app([store], tmp / "missing"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sources(self):
        res = self.client.get("/api/sources").json()
        self.assertEqual(res[0]["name"], "gnb.log")
        ue = next(d for d in res[0]["datasets"] if d["name"] == "sched_ue")
        brate = next(f for f in ue["fields"] if f["name"] == "dl_brate")
        self.assertEqual(brate, {"name": "dl_brate", "type": "number", "unit": "bps"})

    def test_sources_dataset_label_and_instance(self):
        res = self.client.get("/api/sources").json()
        ds = next(d for d in res[0]["datasets"] if d["name"] == "exec")
        self.assertEqual((ds["label"], ds["instance"]), ("executors", "executor"))
        self.assertEqual(ds["t_max"] - ds["t_min"], 9)

    def test_series_instance(self):
        res = self.client.get("/api/series", params={"source": 0, "dataset": "exec", "field": "task_avg", "instance": "du_ctrl_exec"})
        self.assertEqual(res.json()["series"][0]["v"], [20] * 10)

    def test_series(self):
        res = self.client.get("/api/series", params={"source": 0, "dataset": "sched_ue", "field": "dl_brate", "split_by": "ue", "split_values": ["0", "1"]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["series"]), 2)

    def test_series_errors(self):
        self.assertEqual(self.client.get("/api/series", params={"source": 5, "dataset": "mac", "field": "x"}).status_code, 404)
        self.assertEqual(self.client.get("/api/series", params={"source": 0, "dataset": "mac", "field": "x"}).status_code, 400)

    def test_series_filter(self):
        res = self.client.get("/api/series", params={"source": 0, "dataset": "sched_ue", "field": "dl_brate", "split_by": "ue", "filter": "ue == 1"})
        self.assertEqual([s["label"] for s in res.json()["series"]], ["ue=1"])
        res = self.client.get("/api/series", params={"source": 0, "dataset": "sched_ue", "field": "dl_brate", "filter": "ue >"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Filter:", res.json()["detail"])

    def test_stats(self):
        res = self.client.get("/api/stats", params={"source": 0, "dataset": "mac", "field": "wall_clock_latency_max"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["series"][0]["max"], 90)

    def test_histogram(self):
        res = self.client.get("/api/histogram", params={"source": 0, "dataset": "sched_ue", "field": "dl_brate", "bins": 2})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["series"][0]["counts"], [10, 10])

    def test_table(self):
        res = self.client.get("/api/table", params={"source": 0, "dataset": "sched_ue", "limit": 3}).json()
        self.assertEqual((res["total"], len(res["rows"])), (20, 3))
        names = [f["name"] for f in res["fields"]]
        self.assertEqual(names[:3], ["ue", "pci", "rnti"])
        brate = res["fields"][names.index("dl_brate")]
        self.assertEqual((brate["unit"], brate["context"]), ("bps", False))
        self.assertEqual(len(res["rows"][0]), 2 + len(names))

    def test_series_csv(self):
        params = {"source": 0, "dataset": "sched_ue", "field": "dl_brate", "split_by": "ue"}
        res = self.client.get("/api/series.csv", params=params)
        self.assertEqual(res.status_code, 200)
        self.assertIn('filename="gnb_sched_ue_dl_brate.csv"', res.headers["content-disposition"])
        lines = res.text.splitlines()
        self.assertEqual(lines[0], "time_utc,ue,dl_brate_bps")
        # All the samples, one per UE per second, not a downsampled series.
        self.assertEqual(len(lines), 1 + 20)
        self.assertEqual({line.split(",")[1] for line in lines[1:]}, {"0", "1"})
        one_ue = self.client.get("/api/series.csv", params={**params, "split_values": ["1"]}).text.splitlines()
        self.assertEqual(len(one_ue), 1 + 10)
        unsplit = self.client.get("/api/series.csv", params={"source": 0, "dataset": "mac", "field": "nof_slots"}).text
        self.assertEqual(unsplit.splitlines()[0], "time_utc,nof_slots")

    def test_series_csv_of_two_fields(self):
        params = {"source": 0, "dataset": "sched_ue", "field": "dl_brate", "field2": "cqi", "split_by": "ue", "split_values": ["1"]}
        res = self.client.get("/api/series.csv", params=params)
        self.assertIn('filename="gnb_sched_ue_dl_brate_cqi.csv"', res.headers["content-disposition"])
        lines = res.text.splitlines()
        self.assertEqual(lines[0], "time_utc,ue,field,value,unit")
        # The samples of both, in time order, with the unit of each.
        self.assertEqual(len(lines), 1 + 20)
        self.assertEqual([line.split(",")[2] for line in lines[1:3]], ["dl_brate", "cqi"])
        self.assertEqual({line.split(",")[4] for line in lines[1:]}, {"bps", ""})
        self.assertEqual(self.client.get("/api/series.csv", params={**params, "field2": "nope"}).status_code, 400)

    def test_series_csv_errors(self):
        base = {"source": 0, "dataset": "sched_ue", "field": "dl_brate"}
        self.assertEqual(self.client.get("/api/series.csv", params={**base, "filter": "nope > 1"}).status_code, 400)
        self.assertEqual(self.client.get("/api/series.csv", params={**base, "field": "nope"}).status_code, 400)

    def test_table_csv(self):
        params = {"source": 0, "dataset": "exec", "instance": "cell_exec", "fields": ["executor", "task_avg"]}
        res = self.client.get("/api/table.csv", params=params)
        self.assertEqual(res.status_code, 200)
        self.assertIn('filename="gnb_exec_cell_exec.csv"', res.headers["content-disposition"])
        lines = res.text.splitlines()
        self.assertEqual(lines[0], "time_utc,line,executor,task_avg_us")
        self.assertEqual(len(lines), 11)
        self.assertTrue(lines[1].startswith("2026-06-29T14:10:00.000000Z,"))
        self.assertTrue(lines[1].endswith(",cell_exec,10"))

    def test_table_csv_percent_unit(self):
        res = self.client.get("/api/table.csv", params={"source": 0, "dataset": "exec", "fields": ["cpu_load"]})
        self.assertEqual(res.text.splitlines()[0], "time_utc,line,cpu_load_pct")

    def test_table_csv_errors(self):
        base = {"source": 0, "dataset": "mac"}
        self.assertEqual(self.client.get("/api/table.csv", params={**base, "filter": "nope > 1"}).status_code, 400)
        self.assertEqual(self.client.get("/api/table.csv", params={**base, "fields": ["nope"]}).status_code, 400)

    def test_context(self):
        res = self.client.get("/api/context", params={"source": 0, "dataset": "sched_ue", "field": "ue"})
        self.assertEqual(res.json(), [0, 1])

    def test_records(self):
        res = self.client.get("/api/records", params={"source": 0, "around": 2, "count": 1})
        self.assertEqual(res.json()[0]["record"], 2)

    def test_records_search(self):
        lines = self.client.get("/api/records", params={"source": 0, "around": 1, "count": 1000}).json()
        want = [r["record"] for r in lines if "ue=1 " in r["text"]]
        search = lambda **params: self.client.get("/api/records/search", params={"source": 0, **params})
        self.assertEqual(search(q="UE=1 ").json(), {"record": want[0], "wrapped": False})
        self.assertEqual(search(q="ue=1 ", after=want[0]).json()["record"], want[1])
        self.assertEqual(search(q="ue=1 ", after=want[-1]).json(), {"record": want[0], "wrapped": True})
        self.assertEqual(search(q="ue=1 ", after=want[1], backward=True).json()["record"], want[0])
        self.assertEqual(search(q=r"ue=\d pci=1 rnti=0x4601", regex=True).json()["record"], want[0])
        self.assertEqual(search(q="no such text").json(), {"record": None, "wrapped": False})
        self.assertEqual(search(q="(", regex=True).status_code, 400)

    def test_records_marks(self):
        from fastapi.testclient import TestClient

        from viz.registry import SourceRegistry
        from viz.server import create_app
        from viz.sources.log_metrics import LogMetricsSource

        registry = SourceRegistry()
        registry.add_store(self.store, LogMetricsSource())
        client = TestClient(create_app(registry, Path(self.tmp.name) / "missing"))
        lines = client.get("/api/records", params={"source": 0, "around": 1, "count": 20}).json()
        mac = next(r["record"] for r in lines if "MAC cell" in r["text"])
        params = {"source": 0, "around": mac, "count": 3, "mark": mac, "field": "wall_clock_latency_max"}
        res = client.get("/api/records", params=params).json()
        marked = [r for r in res if "marks" in r]
        self.assertEqual([r["record"] for r in marked], [mac])
        (s, e), = marked[0]["marks"]
        self.assertTrue(marked[0]["text"][s:e].startswith("max="))
        # Without the source type, records carry no marks.
        self.assertFalse(any("marks" in r for r in self.client.get("/api/records", params=params).json()))

    def test_static_index(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])

    def test_static_files_are_revalidated(self):
        res = self.client.get("/assets/app.js")
        self.assertEqual(res.headers["cache-control"], "no-cache")
        etag = res.headers["etag"]
        self.assertEqual(self.client.get("/assets/app.js", headers={"if-none-match": etag}).status_code, 304)

    def test_events(self):
        src = self.client.get("/api/sources").json()[0]
        self.assertEqual(src["events_status"], "ready")
        self.assertEqual(src["event_counts"], {"ra": 2, "lifecycle": 2, "failure": 1, "warning": 1})
        res = self.client.get("/api/events", params={"source": 0, "categories": ["failure"]}).json()
        self.assertEqual([e["type"] for e in res["events"]], ["rlf"])
        self.assertEqual(set(res["events"][0]), {"t", "record", "type", "category", "layer", "level", "ue", "rnti", "cause", "text", "lane", "count", "span"})

    def test_trace(self):
        res = self.client.get("/api/trace", params={"source": 0}).json()
        self.assertEqual([(lane["ue"], lane["rnti"], lane["open"]) for lane in res["lanes"]], [(0, "0x4600", True), (1, "0x4601", True)])
        self.assertEqual((res["total_lanes"], res["total_events"], res["truncated"]), (2, 6, False))
        limited = self.client.get("/api/trace", params={"source": 0, "max_lanes": 1, "limit": 2}).json()
        self.assertEqual((len(limited["lanes"]), limited["total_lanes"], len(limited["events"]), limited["truncated"]), (1, 2, 2, True))
        filtered = self.client.get("/api/trace", params={"source": 0, "filter": "rnti == 0x4601"}).json()
        self.assertEqual([lane["rnti"] for lane in filtered["lanes"]], ["0x4601"])
        self.assertTrue(filtered["events"])
        self.assertEqual(self.client.get("/api/trace", params={"source": 0, "filter": "nope == 1"}).status_code, 400)
        grouped = self.client.get("/api/trace", params={"source": 0, "group_by": "du_ue"}).json()
        self.assertEqual([lane["label"] for lane in grouped["lanes"]], ["du_ue=0", "du_ue=1"])

    def test_sources_status(self):
        src = self.client.get("/api/sources").json()[0]
        self.assertEqual((src["status"], src["progress"], src["error"]), ("ready", 1.0, None))
        self.assertEqual(src["notes"], [])

    def test_open_from_page(self):
        from fastapi.testclient import TestClient

        from viz.registry import SourceRegistry
        from viz.server import create_app
        from viz.sources.log_metrics import LogMetricsSource
        from viz.store import StoreCache

        from .test_registry import wait_ready

        tmp = Path(self.tmp.name).resolve()
        (tmp / "logs").mkdir()
        log = write_log(tmp / "logs" / "du.log")
        registry = SourceRegistry(StoreCache(tmp / "cache2"), [LogMetricsSource()], [tmp])
        client = TestClient(create_app(registry, tmp / "missing"))
        self.assertEqual(client.get("/api/roots").json(), {"roots": [str(tmp)], "can_open": True})
        listing = client.get("/api/fs", params={"path": str(tmp / "logs")}).json()
        self.assertEqual([e["name"] for e in listing["entries"]], ["du.log"])
        self.assertEqual(client.get("/api/fs", params={"path": "/etc"}).status_code, 400)
        res = client.post("/api/runs", json={"path": str(log)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.json()["id"], res.json()["kind"], res.json()["sources"]), (0, "file", [0]))
        wait_ready(registry, 0)
        self.assertEqual(client.get("/api/sources").json()[0]["status"], "ready")
        self.assertEqual(client.get("/api/series", params={"source": 0, "dataset": "mac", "field": "nof_slots"}).status_code, 200)
        self.assertEqual(client.post("/api/runs", json={"path": "/etc/hostname"}).status_code, 400)
        self.assertEqual(client.delete("/api/runs/0").json(), {"id": 0})
        self.assertEqual(client.get("/api/runs").json(), [])
        self.assertEqual(client.get("/api/series", params={"source": 0, "dataset": "mac", "field": "nof_slots"}).status_code, 409)
        self.assertEqual(client.delete("/api/runs/9").status_code, 404)

    def test_harq(self):
        import shutil

        from fastapi.testclient import TestClient

        from viz.registry import SourceRegistry
        from viz.server import create_app
        from viz.sources.schedtrace import SchedtraceSource
        from viz.store import StoreCache

        from .test_registry import wait_ready
        from .test_schedtrace_source import TRACE

        tmp = Path(self.tmp.name).resolve()
        shutil.copy(TRACE, tmp / "schedtrace_cell0.bin")
        registry = SourceRegistry(StoreCache(tmp / "cache2"), [SchedtraceSource()], [tmp])
        client = TestClient(create_app(registry, tmp / "missing"))
        client.post("/api/runs", json={"path": str(tmp / "schedtrace_cell0.bin")})
        client.post("/api/sources/0/parse")
        wait_ready(registry, 0)
        res = client.get("/api/harq", params={"source": 0, "record": 2, "item": "ue_pdschs:0"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.json()["dir"], res.json()["harq_id"]), ("DL", 3))
        self.assertEqual([(r["kind"], r["record"], r["slot"]) for r in res.json()["rows"]],
                         [("PDCCH", 2, "1234.5"), ("PDSCH", 2, "1234.5")])
        self.assertEqual(client.get("/api/harq", params={"source": 0, "record": 2, "item": "ssbs:0"}).status_code, 404)
        self.assertEqual(client.get("/api/harq", params={"source": 0, "record": 9, "item": "puschs:0"}).status_code, 400)
        self.assertEqual(client.get("/api/harq", params={"source": 0, "record": 2, "item": "puschs"}).status_code, 422)

    def test_runs_from_page(self):
        from fastapi.testclient import TestClient

        from viz.registry import SourceRegistry
        from viz.server import create_app
        from viz.sources.log_metrics import LogMetricsSource
        from viz.store import StoreCache

        from .test_registry import wait_ready

        tmp = Path(self.tmp.name).resolve()
        run_dir = tmp / "run"
        run_dir.mkdir()
        write_log(run_dir / "du.log")
        write_log(run_dir / "cu.log")
        (run_dir / "notes.txt").write_text("not a log")
        registry = SourceRegistry(StoreCache(tmp / "cache2"), [LogMetricsSource()], [tmp])
        client = TestClient(create_app(registry, tmp / "missing"))
        run = client.post("/api/runs", json={"path": str(run_dir)}).json()
        self.assertEqual((run["name"], run["kind"], run["sources"]), ("run/", "dir", [0, 1]))
        self.assertEqual([s["file"] for s in client.get("/api/sources").json()], ["cu.log", "du.log"])
        for source_id in run["sources"]:
            wait_ready(registry, source_id)

        res = client.delete(f"/api/runs/{run['id']}/sources/0").json()
        self.assertEqual(res["sources"], [1])
        files = client.get(f"/api/runs/{run['id']}/files").json()
        self.assertEqual([(f["name"], f["in_run"]) for f in files], [("cu.log", False), ("du.log", True)])
        res = client.post(f"/api/runs/{run['id']}/sources", json={"path": str(run_dir / "cu.log")})
        self.assertEqual(res.json()["sources"], [1, 2])
        wait_ready(registry, 2)
        other = write_log(tmp / "other.log")
        self.assertEqual(client.post(f"/api/runs/{run['id']}/sources", json={"path": str(other)}).status_code, 400)

        single = client.post("/api/runs", json={"path": str(run_dir / "du.log")}).json()
        self.assertEqual(single["kind"], "file")
        related = client.get(f"/api/runs/{single['id']}/related").json()
        self.assertEqual([f["name"] for f in related], ["cu.log"])
        # The directory is open already, so the file run closes in favour of it.
        promoted = client.post(f"/api/runs/{single['id']}/promote").json()
        self.assertEqual(promoted["id"], run["id"])
        self.assertEqual([r["id"] for r in client.get("/api/runs").json()], [run["id"]])
        self.assertEqual(client.get("/api/runs/9/related").status_code, 404)
        self.assertEqual(client.post("/api/runs", json={"path": str(tmp / "cache2")}).status_code, 400)

    def test_open_without_registry(self):
        self.assertEqual([(r["name"], r["kind"], r["sources"]) for r in self.client.get("/api/runs").json()], [("gnb.log", "file", [0])])
        res = self.client.post("/api/runs", json={"path": "/tmp"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.client.get("/api/roots").json()["can_open"], False)

    def test_frontend_not_built(self):
        res = self.not_built_client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("not built", res.text)
        self.assertEqual(self.not_built_client.get("/api/sources").status_code, 200)


if __name__ == "__main__":
    unittest.main()
