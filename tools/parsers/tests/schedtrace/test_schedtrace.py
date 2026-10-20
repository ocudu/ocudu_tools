# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from parsers import schedtrace
from parsers.schedtrace import _generated, codegen, grid, schema

# Trace built by data/make_fixtures.sh with flatc from the JSON events, and flatc's decoding of it.
DATA = Path(__file__).parent / "data"
EVENTS = ("cell_start", "cell_slot", "cell_stop")


def from_flatc(value):
    """flatc's JSON as the decoder gives it: unions with their type in."""
    if isinstance(value, list):
        return [from_flatc(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, v in value.items():
        if key.endswith("_type") and key[: -len("_type")] in value:
            continue
        kind = value.get(f"{key}_type")
        if isinstance(v, dict) and kind is not None:
            v = {"type": kind, **v}
        elif isinstance(v, list) and kind is not None:
            v = [{"type": k, **e} for k, e in zip(kind, v)]
        out[key] = from_flatc(v)
    return out


def present(value):
    """A value without its absent fields, which flatc leaves out or gives as null."""
    if isinstance(value, dict):
        return {k: present(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [present(v) for v in value]
    return value


class SchedtraceTest(unittest.TestCase):
    def test_events_match_flatc(self):
        events = list(schedtrace.read(DATA / "schedtrace_cell0.bin"))
        self.assertEqual([e["type"] for e in events], ["CellStartEvent", "CellSlotEvent", "CellStopEvent"])
        for name, event in zip(EVENTS, events):
            ref = from_flatc(json.loads((DATA / f"{name}.flatc.json").read_text()))
            value = ref.pop("value")
            ref = {**ref, **value}
            self.assertEqual(present(event), present(ref), name)

    def test_slot_event(self):
        slot = list(schedtrace.read(DATA / "schedtrace_cell0.bin"))[1]
        self.assertEqual((slot["slot_tx"], slot["latency_us"]), (12345, 42))
        # A vector of unions, with structs inline and enums by name.
        rach, harq, sr, csi = slot["inputs"]
        self.assertEqual(rach["occasions"][0]["preambles"], [{"preamble_id": 23, "tc_rnti": 17922, "time_advance": -5}])
        self.assertEqual((harq["type"], harq["status"]), ("HarqAckEvent", "Dtx"))
        self.assertEqual(sr, {"type": "SrEvent", "slot_rx": 12342, "ue_idx": 1, "rnti": 17922})
        # Optional scalars are None when absent.
        self.assertEqual((csi["ri"], csi["li"], csi["pmi"]["type"], csi["pmi"]["i_1_2"]), (2, None, "PmiTypeISinglePanel", None))
        decision = slot["decision"]
        self.assertEqual(decision["ue_pdschs"][0]["rbs"], {"type": "RbgAlloc", "nof_rbgs": 9, "bitmap": 511})
        self.assertEqual(decision["rar_pdschs"][0]["grants"][0]["tpc"], -1)
        self.assertIsNone(decision["ul_pdcchs"])

    def test_incomplete_last_event(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        self.assertEqual(len(list(schedtrace.events(data[:-5]))), 2)
        self.assertEqual(list(schedtrace.events(b"")), [])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(data[:-5])
            self.assertEqual(len(list(schedtrace.read(path))), 2)

    def test_corrupt_size_warns(self):
        data = bytearray((DATA / "schedtrace_cell0.bin").read_bytes())
        second = schedtrace.offsets(data)[1]
        data[second : second + 4] = (schedtrace.MAX_EVENT_SIZE + 1).to_bytes(4, "little")
        with self.assertWarnsRegex(UserWarning, f"corrupt schedtrace event size .* at offset {second}"):
            self.assertEqual(len(list(schedtrace.events(data))), 1)
        with self.assertWarnsRegex(UserWarning, "corrupt"):
            self.assertEqual(schedtrace.offsets(data), [schedtrace.HEADER.size])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(data)
            with self.assertWarnsRegex(UserWarning, f"{path}: corrupt"):
                self.assertEqual(len(list(schedtrace.read(path))), 1)

    def test_zero_tail_warns(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        size = len(data)
        data += bytes(4096)
        with self.assertWarnsRegex(UserWarning, f"corrupt schedtrace event size 0 at offset {size}"):
            self.assertEqual(len(list(schedtrace.events(data))), 3)
        with self.assertWarnsRegex(UserWarning, "corrupt"):
            self.assertEqual(schedtrace.offsets(data), schedtrace.offsets(data[:size]))
        with self.assertRaises(schedtrace.CorruptEventError):
            schedtrace.event_at(data, size)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(data)
            with self.assertWarnsRegex(UserWarning, f"{path}: corrupt"):
                self.assertEqual(len(list(schedtrace.read(path))), 3)

    def test_corrupt_root_warns(self):
        data = bytearray((DATA / "schedtrace_cell0.bin").read_bytes())
        second = schedtrace.offsets(data)[1]
        data[second + 4 : second + 8] = (1 << 31).to_bytes(4, "little")
        with self.assertWarnsRegex(UserWarning, f"corrupt schedtrace event at offset {second}"):
            self.assertEqual(len(list(schedtrace.events(data))), 1)
        with self.assertWarnsRegex(UserWarning, "corrupt"):
            self.assertEqual(schedtrace.offsets(data), [schedtrace.HEADER.size])
        with self.assertRaisesRegex(schedtrace.CorruptEventError, f"offset {second}"):
            schedtrace.event_at(data, second)

    def test_event_at_an_offset_past_the_end(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        for offset in (len(data) - 2, len(data) + 10):
            with self.assertRaises(schedtrace.CorruptEventError):
                schedtrace.event_at(data, offset)

    def test_garbled_bytes_do_not_raise(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        # Each byte of the events garbled, the decoding stops at the corrupt event or decodes wrong values.
        for pos in range(schedtrace.HEADER.size, len(data)):
            for value in (0x00, 0xFF):
                garbled = data[:pos] + bytes([value]) + data[pos + 1 :]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    for grid_only in (False, True):
                        list(schedtrace.events(garbled, grid_only))
                    for offset in schedtrace.offsets(garbled):
                        try:
                            schedtrace.event_at(garbled, offset)
                        except schedtrace.CorruptEventError:
                            pass

    def test_offsets_stop_at_the_end_of_the_contents(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        self.assertEqual(schedtrace.offsets(data[:-5], end=len(data)), schedtrace.offsets(data[:-5]))

    def test_read_matches_events(self):
        data = (DATA / "schedtrace_cell0.bin").read_bytes()
        self.assertEqual(list(schedtrace.read(DATA / "schedtrace_cell0.bin")), list(schedtrace.events(data)))

    def test_fields_of_newer_schemas_are_ignored(self):
        with self.assertWarnsRegex(UserWarning, "format 1.1 is newer than 1.0"):
            (stop,) = schedtrace.read(DATA / "cell_stop_newer.bin")
        self.assertEqual(stop, {"timestamp_us": 1, "type": "CellStopEvent", "cell_idx": 3})

    def test_schema(self):
        tables = schedtrace.decoder().schema.tables
        # A union field takes two slots, its type first.
        self.assertEqual([(f.name, f.slot) for f in tables["CellSlotEvent"].fields],
                         [("slot_tx", 0), ("latency_us", 1), ("inputs", 3), ("decision", 4)])
        self.assertIsNone(next(f for f in tables["VrbAlloc"].fields if f.name == "vrb_second_hop_start").default)

    def schema_of(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.fbs"
            path.write_text(text)
            return schema.load(path)

    def test_schema_syntax(self):
        s = self.schema_of("enum E : ubyte { A = 0x10, B }\nunion U { T, V = 4 }\ntable T { e: E = B; h: int = 0x1e; }\n"
                           "table V {}\n")
        self.assertEqual(s.enums["E"].names, {16: "A", 17: "B"})
        self.assertEqual(s.unions["U"].members, {1: "T", 4: "V"})
        self.assertEqual([f.default for f in s.tables["T"].fields], [17, 30])

    def test_unsupported_schema_syntax_is_rejected(self):
        for text in ("union U { T1: T }\ntable T {}\n", "enum E : ubyte { A B }\n", "enum E : ubyte { A }\ntable T { e: E = C; }\n",
                     "table T { a: int (id: 1); b: int (id: 0); }\n"):
            with self.assertRaises(ValueError, msg=text):
                self.schema_of(text)

class FormatVersionTest(unittest.TestCase):
    def setUp(self):
        self.data = (DATA / "schedtrace_cell0.bin").read_bytes()

    def with_version(self, major, minor):
        return schedtrace.HEADER.pack(schedtrace.MAGIC, major, minor) + self.data[schedtrace.HEADER.size :]

    def test_format_version_of_the_schemas(self):
        self.assertEqual(codegen.format_version(schedtrace.decoder().schema), schedtrace.FORMAT_VERSION)

    def test_version(self):
        self.assertEqual(schedtrace.version(self.data), schedtrace.FORMAT_VERSION)
        self.assertIsNone(schedtrace.version(self.data[schedtrace.HEADER.size :]))
        self.assertIsNone(schedtrace.version(b"SCHT"))

    def test_trace_without_header_is_rejected(self):
        with self.assertRaisesRegex(schedtrace.FormatError, "header is missing"):
            list(schedtrace.events(self.data[schedtrace.HEADER.size :]))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedtrace_cell0.bin"
            path.write_bytes(self.data[schedtrace.HEADER.size :])
            with self.assertRaises(schedtrace.FormatError):
                list(schedtrace.read(path))

    def test_other_major_is_rejected(self):
        major, minor = schedtrace.FORMAT_VERSION
        for version in [(major + 1, 0), *([(major - 1, minor)] if major > 0 else [])]:
            with self.assertRaisesRegex(schedtrace.FormatError, "is not supported"):
                schedtrace.offsets(self.with_version(*version))

    def test_older_minor_is_accepted(self):
        major, minor = schedtrace.FORMAT_VERSION
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(len(list(schedtrace.events(self.with_version(major, max(0, minor - 1))))), 3)

    def test_partial_header_has_no_events(self):
        for head in (b"", b"S", b"SCHT\x01"):
            self.assertEqual(list(schedtrace.events(head)), [])
            self.assertEqual(schedtrace.offsets(head), [])


class GeneratedTest(unittest.TestCase):
    def test_up_to_date(self):
        self.assertEqual(_generated.SCHEMA_HASH, codegen.source_hash(),
                         "_generated.py is stale, regenerate it with python -m parsers.schedtrace.codegen")

    def test_matches_decoder(self):
        dec = schedtrace.decoder()
        for name in ("schedtrace_cell0.bin", "cell_stop_newer.bin"):
            data = memoryview((DATA / name).read_bytes())
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                offsets = schedtrace.offsets(data)
            for offset in offsets:
                size = int.from_bytes(data[offset : offset + 4], "little")
                buf = data[offset + 4 : offset + 4 + size]
                # Compared as lists of items, so that the order of the fields matters too.
                self.assertEqual(list(_generated.root(buf).items()), list(dec.root(buf).items()), name)

    def test_grid_only(self):
        full = list(schedtrace.read(DATA / "schedtrace_cell0.bin"))
        brief = list(schedtrace.read(DATA / "schedtrace_cell0.bin", grid_only=True))
        cell = grid.cell_of(full[0])
        self.assertEqual(brief[0], full[0])
        self.assertEqual(grid.allocations(brief[1], cell), grid.allocations(full[1], cell))
        self.assertEqual([i["type"] for i in brief[1]["inputs"]], [i["type"] for i in full[1]["inputs"]])
        self.assertEqual((brief[1]["slot_tx"], brief[1]["latency_us"]), (full[1]["slot_tx"], full[1]["latency_us"]))
        self.assertNotIn("srss", brief[1]["decision"])
        self.assertEqual(brief[2], full[2])

    def test_grid_fields_exist(self):
        tables = schedtrace.decoder().schema.tables
        for name, fields in codegen.GRID_FIELDS.items():
            self.assertLessEqual(set(fields), {f.name for f in tables[name].fields}, name)


class GridTest(unittest.TestCase):
    def setUp(self):
        start, self.slot, _ = schedtrace.read(DATA / "schedtrace_cell0.bin")
        self.cell = grid.cell_of(start)

    def test_allocations(self):
        rects = grid.allocations(self.slot, self.cell)
        by_channel = {}
        for r in rects:
            by_channel.setdefault(r["channel"], []).append((r["dir"], r["sym_start"], r["sym_length"], r["crb_start"], r["crb_length"]))
        # 9 RBGs of 4 PRBs in a 52-PRB BWP starting at CRB 2, the first RBG cut short at CRB 4, merged.
        self.assertEqual(by_channel["PDSCH"], [("DL", 1, 13, 2, 34)])
        self.assertEqual(by_channel["PUSCH"], [("UL", 0, 14, 5, 20)])
        # The RAR PDSCH of the trace has no symbols, so no rectangle.
        self.assertNotIn("RAR", by_channel)
        self.assertEqual(by_channel["SSB"], [("DL", 2, 4, 10, 20)])
        pdsch = next(r for r in rects if r["channel"] == "PDSCH")
        self.assertEqual((pdsch["rnti"], pdsch["mcs"], pdsch["tbs"], pdsch["item"]), (17921, 27, 12345, ["ue_pdschs", 0]))

    def test_pucch_hops(self):
        slot = {"decision": {"pucchs": [{"rnti": 1, "res_idx": 1, "nof_bits_harq_ack": 2, "nof_bits_sr": 0, "nof_bits_csi1": 0}]}}
        rects = grid.allocations(slot, self.cell)
        # Resource 1: format 2 of 2 PRBs at PRB 40, hopping to PRB 7 for the second of its 2 symbols.
        self.assertEqual([(r["sym_start"], r["sym_length"], r["crb_start"], r["crb_length"]) for r in rects], [(12, 1, 40, 2), (13, 1, 7, 2)])

    def test_csi_rs_is_clipped_to_the_bwp(self):
        # CRBs 0 to 60 in the DL BWP of CRBs 2 to 54.
        slot = {"decision": {"csi_rs": [{"symbol0": 5, "crbs": {"start": 0, "length": 60}, "row": 1}]}}
        rects = grid.allocations(slot, self.cell)
        self.assertEqual([(r["sym_start"], r["crb_start"], r["crb_length"]) for r in rects], [(5, 2, 52)])

    def test_long_prach_start_symbol_counts_15khz_symbols(self):
        # In a 30 kHz UL BWP: format 0 from 15 kHz symbol 7, the first symbol of the second slot of the subframe, and
        # format 3 from 15 kHz symbol 2, 30 kHz symbol 4.
        slot = {"decision": {"prachs": [{"format": 0, "start_symbol": 7, "nof_occasions": 1, "index_fd_ra": 0},
                                        {"format": 3, "start_symbol": 2, "nof_occasions": 1, "index_fd_ra": 0}]}}
        rects = grid.allocations(slot, self.cell)
        self.assertEqual([(r["sym_start"], r["sym_length"], r["crb_start"], r["crb_length"]) for r in rects],
                         [(0, 28, 3, 3), (4, 28, 3, 12)])

    def test_occupancy(self):
        rects = [{"channel": "PDSCH", "crb_start": 2, "crb_length": 6, "sym_start": 0, "sym_length": 7}]
        self.assertEqual(grid.occupancy(rects, 4), {(0, "PDSCH"): 0.25, (1, "PDSCH"): 0.5})

    def test_rbg_size(self):
        self.assertEqual([grid.rbg_size(n) for n in (24, 52, 106, 273)], [2, 4, 8, 16])

    def test_rbgs_past_the_bwp_hold_no_crbs(self):
        bwp = {"crb_start": 0, "crb_length": 52}
        # 13 RBGs of 4 CRBs fill the BWP; 20 set bits, as of a corrupt bitmap, give no more.
        self.assertEqual(grid.rbg_crbs((1 << 20) - 1, 20, bwp), [(0, 52)])
