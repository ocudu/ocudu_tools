# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from parsers.log import adhoc, chunks

LOG = """\
2026-10-12T13:35:41.000000 [SCHED   ] [I] Slot decisions pci=1 (2 PDSCH): DL: ue=0 c-rnti=0x44 tbs=5889 DL: ue=1 c-rnti=0x45 tbs=12
2026-10-12T13:35:41.000500 [SCHED   ] [I] Slot decisions pci=1 (1 PDSCH): DL: ue=1 c-rnti=0x45 tbs=300 newtx=true
2026-10-12T13:35:41.001000 [METRICS ] Scheduler UE ue=0 rnti=0x44 metrics: dl_brate=55.5Mbps ul_brate=1.2kbps mytbs=7
2026-10-12T13:35:41.001500 [RRC     ] [D] DL rrcReconfiguration:
  tbs=99 in the body of an entry
2026-10-12T13:35:41.002000 [SCHED   ] [I] tbs=n/a dl_tbs=4 tbs=-3
"""


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class AdhocTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "gnb.log"
        self.path.write_text(LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, spec):
        return adhoc.scan(str(self.path), 0, self.path.stat().st_size, 1, spec)

    def test_values(self):
        res = self.scan(adhoc.Spec("tbs"))
        # Whole keys only (not mytbs or dl_tbs), numbers only (not n/a), with the time of their entry: the body line takes
        # the time of its header.
        self.assertEqual([(line, value) for _, line, _, value, _ in res["rows"]], [(1, 5889), (1, 12), (2, 300), (5, 99), (6, -3)])
        self.assertEqual(res["rows"][3][0], epoch("2026-10-12T13:35:41.001500"))
        self.assertIsNone(res["unit"])
        offsets = [offset for _, _, offset, _, _ in res["rows"]]
        self.assertEqual(LOG.encode()[offsets[2]:].split(b"\n")[0], LOG.splitlines()[1].encode())

    def test_filter_and_split(self):
        res = self.scan(adhoc.Spec("tbs", filter="slot DECISIONS", split_key="ue"))
        # Each value takes the nearest ue= before it in its line; the filter is case-insensitive.
        self.assertEqual([(value, split) for *_, value, split in res["rows"]], [(5889, "0"), (12, "1"), (300, "1")])

    def test_units(self):
        res = self.scan(adhoc.Spec("dl_brate"))
        self.assertEqual(([r[3] for r in res["rows"]], res["unit"]), ([55500000], "bps"))

    def test_chunks_give_the_same_rows(self):
        ranges = chunks.chunk_ranges(self.path, 3)
        self.assertGreater(len(ranges), 1)
        rows = []
        for (start, end), first in zip(ranges, chunks.first_lines(self.path, ranges)):
            rows += adhoc.scan(str(self.path), start, end, first, adhoc.Spec("tbs", split_key="ue"))["rows"]
        self.assertEqual(rows, self.scan(adhoc.Spec("tbs", split_key="ue"))["rows"])

    def test_lines(self):
        rows = adhoc.scan_lines(str(self.path), 0, self.path.stat().st_size, 1, adhoc.Spec(None, filter="TBS=\\d"))
        # Each matching line once, case-insensitive, also within longer keys as a regex does, the body line with the time
        # of its entry.
        self.assertEqual([line for _, line, *_ in rows], [1, 2, 3, 5, 6])
        t, line, offset, text, layer, level, ue, rnti = rows[0]
        self.assertEqual((layer, level, ue, rnti), ("SCHED", "I", 0, "0x44"))
        self.assertEqual(text, LOG.splitlines()[0])
        self.assertEqual(rows[3][0], epoch("2026-10-12T13:35:41.001500"))
        self.assertEqual(rows[3][4:], (None, None, None, None))

    def test_crlf_lines(self):
        crlf = Path(self.tmp.name) / "crlf.log"
        crlf.write_bytes(LOG.replace("\n", "\r\n").encode())
        size = crlf.stat().st_size
        res = adhoc.scan(str(crlf), 0, size, 1, adhoc.Spec("tbs", filter=r"tbs=\d+$"))
        self.assertEqual([(line, value) for _, line, _, value, _ in res["rows"]], [(1, 5889), (1, 12)])
        rows = adhoc.scan_lines(str(crlf), 0, size, 1, adhoc.Spec(None, filter=r"tbs=\d+$"))
        self.assertEqual([(line, text) for _, line, _, text, *_ in rows], [(1, LOG.splitlines()[0]), (3, LOG.splitlines()[2])])

    def test_crlf_anchors(self):
        for pattern, want in [
            (r"a$", r"a(?=\r?$)"),
            (r"a\$", r"a\$"),
            (r"[$]$", r"[$](?=\r?$)"),
            (r"[]$]$", r"[]$](?=\r?$)"),
            (r"[^]$]$", r"[^]$](?=\r?$)"),
            (r"(?#$)$", r"(?#$)(?=\r?$)"),
        ]:
            with self.subTest(pattern=pattern):
                self.assertEqual(adhoc.crlf_anchors(pattern), want)

    def test_lines_take_the_rnti_of_a_ue(self):
        self.path.write_text(
            "2026-10-12T13:35:41.000000 [SCHED   ] [D] RAR allocation for ra-rnti=0x10b was postponed.\n"
            "2026-10-12T13:35:41.000500 [SCHED   ] [I] Processed slot events pci=1: prach(ra-rnti=0x10b preamble=1 tc-rnti=0x4601)\n"
            "2026-10-12T13:35:41.001000 [MAC     ] [I] ue=0 crnti=0x4602: proc started\n")
        rows = adhoc.scan_lines(str(self.path), 0, self.path.stat().st_size, 1, adhoc.Spec(None, filter="rnti"))
        # Not the RA-RNTI of a random access, which is no UE's.
        self.assertEqual([row[-1] for row in rows], [None, "0x4601", "0x4602"])

    def test_lines_of_long_entries_take_the_time_of_their_entry(self):
        log = []
        for s in range(2):
            log.append(f"2026-10-12T13:35:4{s}.000000 [SCHED   ] [D] Slot decisions:")
            log += [f"- ue={i} tbs={i}" for i in range(3)]
        self.path.write_text("\n".join(log) + "\n")
        times = [epoch("2026-10-12T13:35:40"), epoch("2026-10-12T13:35:41")]
        expected = [(times[0], 2), (times[0], 3), (times[0], 4), (times[1], 6), (times[1], 7), (times[1], 8)]
        self.assertEqual([(t, line) for t, line, *_ in self.scan(adhoc.Spec("tbs"))["rows"]], expected)
        rows = adhoc.scan_lines(str(self.path), 0, self.path.stat().st_size, 1, adhoc.Spec(None, filter="tbs="))
        self.assertEqual([(t, line) for t, line, *_ in rows], expected)

    def test_empty_ranges_and_empty_matches(self):
        self.path.write_text("")
        self.assertEqual(self.scan(adhoc.Spec("tbs"))["rows"], [])
        self.assertEqual(adhoc.scan_lines(str(self.path), 0, 0, 1, adhoc.Spec(None, filter="tbs")), [])
        self.path.write_text(LOG)
        # A filter matching empty gives each line once, none past the end.
        rows = adhoc.scan_lines(str(self.path), 0, self.path.stat().st_size, 1, adhoc.Spec(None, filter="^"))
        self.assertEqual([line for _, line, *_ in rows], [1, 2, 3, 4, 5, 6])

    def test_invalid_specs(self):
        for kwargs in ({"key": "a b"}, {"key": "tbs", "filter": "(("}, {"key": "tbs", "split_key": "u e"},
                       {"key": None}, {"key": None, "filter": "x", "split_key": "ue"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                adhoc.Spec(**kwargs)
        self.assertEqual(adhoc.Spec("cw[0].tbs").key, "cw[0].tbs")
        with self.assertRaises(ValueError):
            self.scan(adhoc.Spec(None, filter="tbs"))


if __name__ == "__main__":
    unittest.main()
