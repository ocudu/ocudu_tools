# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import json
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path

from parsers import schedtrace

from viz.sources.schedtrace import SchedtraceSource, _slot_times
from viz.store import StoreCache

# Trace of a cell start, a slot and a cell stop, of the tests of parsers.schedtrace.
TRACE = Path(__file__).resolve().parents[2] / "parsers" / "tests" / "schedtrace" / "data" / "schedtrace_cell0.bin"


def garble_last_event(data):
    """data with a byte of its last event garbled so that its size and root stay valid but its fields do not decode."""
    offsets = schedtrace.offsets(data)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for pos in range(offsets[-1] + 4, len(data)):
            garbled = data[:pos] + b"\xff" + data[pos + 1:]
            if schedtrace.offsets(garbled) != offsets:
                continue
            try:
                schedtrace.event_at(garbled, offsets[-1], grid_only=True)
            except schedtrace.CorruptEventError:
                return garbled
    raise AssertionError("no garbling of the last event that only its fields show")


class SchedtraceSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "schedtrace_cell0.bin"
        shutil.copy(TRACE, self.path)
        self.source = SchedtraceSource()
        self.store = StoreCache(self.dir / "cache").open(self.path, self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts(self):
        self.assertTrue(self.source.accepts(self.path))
        other = self.dir / "trace.bin"
        shutil.copy(TRACE, other)
        self.assertFalse(self.source.accepts(other))
        self.assertTrue(self.source.on_request(self.path))

    def test_grid_meta(self):
        meta = self.store.meta["grid"]
        # 15 kHz subcarriers: 1 ms slots.
        self.assertEqual((meta["nof_crbs"], meta["slot_duration_s"], meta["group_size"]), (54, 0.001, 4))
        # Its only slot has both PDSCH and PUSCH, which only FDD cells have.
        self.assertEqual(meta["duplex"], "FDD")
        self.assertEqual(self.store.meta["t_min"], 1700000000.0)
        # The end of the last slot, the stop event's time plus a slot.
        self.assertAlmostEqual(self.store.meta["t_max"], 1700000000.002, places=6)

    def test_ue_datasets(self):
        datasets = self.store.datasets
        # The slot has a UE PDSCH and a PUSCH, HARQ-ACK and CSI reports, and an SR.
        self.assertEqual(sorted(datasets), ["cell_metrics", "csi_reports", "dl_grants", "harq_acks", "ue_metrics",
                                            "ul_grants"])
        ue = datasets["ue_metrics"]
        self.assertEqual((ue["units"]["dl_brate"], ue["context"], ue["label"]), ("bps", ["rnti", "ue_index"],
                                                                                 "UE metrics per 10 ms"))
        series = self.store.series("dl_grants", "tbs", split_by="rnti")
        # The slot is record 2, after the cell start event.
        self.assertEqual([(s["split"], s["v"], s["record"]) for s in series["series"]], [("0x4601", [12345], [2])])
        self.assertEqual(datasets["dl_grants"]["fields"]["new_data"], "number")
        # Fields without values, e.g. the MCS of the second codewords of PDSCHs of one codeword, are numbers.
        self.assertEqual(datasets["dl_grants"]["fields"]["mcs2"], "number")
        self.assertEqual(self.store.series("dl_grants", "mcs2")["series"], [])
        self.assertEqual(datasets["harq_acks"]["fields"]["status"], "text")

    def test_grid_window(self):
        res = self.source.grid(self.path, self.store, None, None, 1000)
        self.assertEqual((res["mode"], res["nof_slots"]), ("rects", 1))
        self.assertEqual(sorted({r["channel"] for r in res["rects"]}), ["PDCCH", "PDSCH", "PUSCH", "SSB"])
        pdsch = next(r for r in res["rects"] if r["channel"] == "PDSCH")
        self.assertEqual((pdsch["record"], pdsch["t"], pdsch["t1"]), (2, 1700000000.0005, 1700000000.0015))
        self.assertEqual(res["anchors"], [[1700000000.0005, 12345]])
        self.assertEqual(self.store.meta["grid"]["slots_per_frame"], 10)
        self.assertEqual(self.source.grid(self.path, self.store, 0, 1, 1000)["rects"], [])

    def test_records(self):
        texts = [r["text"] for r in self.store.records(1, 10)]
        self.assertEqual(len(texts), 3)
        self.assertTrue(texts[0].endswith("CellStartEvent"))
        # slot_tx 12345 of 10 slots per frame.
        self.assertIn("slot=1234.5 latency=42us", texts[1])
        self.assertEqual(json.loads(self.source.record_detail(self.path, 3))["type"], "CellStopEvent")
        # The structure of the event too, which the record pane shows as tables.
        slot = self.source.record_data(self.path, 2)
        self.assertEqual((slot["type"], len(slot["inputs"]), len(slot["decision"]["ue_pdschs"])), ("CellSlotEvent", 4, 1))

    def test_corrupt_last_event(self):
        path = self.dir / "garbled" / "schedtrace_cell0.bin"
        path.parent.mkdir()
        path.write_bytes(garble_last_event(TRACE.read_bytes()))
        self.assertIsNone(self.source.run_identity(path))
        with self.assertWarnsRegex(UserWarning, "corrupt schedtrace event at offset .*, the events from it are left out"):
            store = StoreCache(self.dir / "cache2").open(path, self.source)
        # The cell start and the slot, before the corrupt stop event.
        self.assertEqual(len(store.records(1, 10)), 2)
        self.assertEqual(self.store.datasets.keys(), store.datasets.keys())
        with self.assertRaisesRegex(ValueError, "Event 3 does not decode"):
            self.source.record_data(path, 3)


class SlotTimesTest(unittest.TestCase):
    def test_slots_on_a_grid(self):
        # Timestamps that jitter around 1 ms slots, across an SFN wrap of 100 slots.
        slots = [(10.0001, 98), (10.0012, 99), (10.0019, 0), (10.0035, 1)]
        times = _slot_times(slots, 0.001, 100)
        steps = [round(b - a, 9) for a, b in zip(times, times[1:])]
        self.assertEqual(steps, [0.001, 0.001, 0.001])
        # At the median difference between the timestamps and the grid.
        self.assertAlmostEqual(times[0], 10.0002, places=6)
        self.assertEqual(_slot_times([], 0.001, 100), [])
