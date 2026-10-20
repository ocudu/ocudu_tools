# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import unittest

from parsers.log import search

LOG = """\
2026-07-27T15:44:57.314934 [GNB     ] [I] Built in RelWithDebInfo mode using commit 404c93d34d on branch main
2026-07-27T15:44:57.315000 [CONFIG  ] [D] gNB input configuration (all values):
log:
  all_level: info

2026-07-27T15:45:01.100000 [RRC     ] [I] ue=0 c-rnti=0x4601: Tx DL-DCCH rrcReconfiguration
  rrcReconfiguration {
    reconfigurationWithSync
  }
2026-07-27T15:45:02.200000 [SCHED   ] [W] [  12.3] pci=1 rnti=0x4602: late HARQ
2026-07-27T15:45:03.300000 [METRICS ] Scheduler UE ue=1 pci=1 rnti=0x4602 metrics: dl_mcs=27
2026-07-28T09:00:00.000000 [MAC     ] [I] [  20.1] ra-rnti=0x4602: Discarding RAR. Cause: no PDCCH
""".splitlines(keepends=True)


def found(flt, include_config_echo=False):
    return [entry.splitlines()[0][27:37] for entry in search.search(LOG, flt, include_config_echo)]


class SearchTest(unittest.TestCase):
    def test_entries(self):
        entries = list(search.entries(LOG))
        self.assertEqual(len(entries), 5)
        # The configuration echo is left out, and continuation lines are kept with their entry.
        self.assertEqual(entries[1][1:], ["  rrcReconfiguration {", "    reconfigurationWithSync", "  }"])
        self.assertEqual(len(list(search.entries(LOG, include_config_echo=True))), 6)

    def test_crlf_entries(self):
        crlf = [line.replace("\n", "\r\n") for line in LOG]
        self.assertEqual(list(search.entries(crlf)), list(search.entries(LOG)))
        self.assertEqual([e.splitlines()[0][11:21] for e in search.search(crlf, search.Filter(pattern="late HARQ$"))], ["15:45:02.2"])

    def test_echo_of_other_nodes_is_left_out(self):
        header = "2026-07-27T15:44:57.315000 [CONFIG  ] [I] Input configuration (only non-default values):"
        self.assertEqual(len(list(search.entries([LOG[0], header, *LOG[2:]]))), 5)

    def test_filters(self):
        self.assertEqual(found(search.Filter(layer="rrc")), ["[RRC     ]"])
        self.assertEqual(found(search.Filter(level="W|E")), ["[SCHED   ]"])
        self.assertEqual(found(search.Filter(ue="1")), ["[METRICS ]"])
        self.assertEqual(found(search.Filter(rnti="4602")), ["[SCHED   ]", "[METRICS ]"])
        # Any form of an RNTI: with 0x, as events give it, or decimal, as F1AP pcaps hold it.
        self.assertEqual(found(search.Filter(rnti="0x4602")), ["[SCHED   ]", "[METRICS ]"])
        self.assertEqual(found(search.Filter(rnti="17922")), ["[SCHED   ]", "[METRICS ]"])
        with self.assertRaises(ValueError):
            search.Filter(rnti="xyz")
        self.assertEqual(found(search.Filter(rnti="4601")), ["[RRC     ]"])
        # Not the RA-RNTI of a random access, nor the temporary C-RNTI of a debug PRACH line.
        self.assertFalse(search.Filter(rnti="10b").matches(
            ["2026-07-27T15:45:00.000000 [SCHED   ] [D] [  10.1] Processed slot events pci=1:",
             "- PRACH: slot=10.1 preamble=12 ra-rnti=0x10b temp_crnti=0x10b ta_cmd=3"]))
        self.assertEqual(found(search.Filter(pci="1", layer="SCHED")), ["[SCHED   ]"])
        # The C-RNTI of the MAC and DU-F1, printed as crnti.
        self.assertTrue(search.Filter(rnti="4601").matches(
            ["2026-07-27T15:45:00.000000 [DU-F1   ] [W] ue=0 crnti=0x4601: F1 UE context failed to be created"]))

    def test_time_bounds(self):
        self.assertEqual(found(search.Filter(after="15:45:02", before="15:45:02.9")), ["[SCHED   ]"])
        self.assertEqual(found(search.Filter(after="2026-07-27T15:45:03")), ["[METRICS ]", "[MAC     ]"])
        self.assertEqual(found(search.Filter(after="2026-07-27T15:45:02", before="2026-07-27T15:45:02")), ["[SCHED   ]"])
        # A space between the date and the time, as str(datetime) gives.
        self.assertEqual(found(search.Filter(after="2026-07-27 15:45:02", before="2026-07-27 15:45:02")), ["[SCHED   ]"])
        # A date alone bounds whole days.
        self.assertEqual(found(search.Filter(after="2026-07-28")), ["[MAC     ]"])
        self.assertEqual(len(found(search.Filter(before="2026-07-27"))), 4)
        self.assertEqual(len(found(search.Filter(after="2026-07-27", before="2026-07-27 15:45:01.5"))), 2)

    def test_iso_bound(self):
        self.assertEqual(search.iso_bound("2026-07-27 15:45:02.5"), "2026-07-27T15:45:02.5")
        self.assertEqual(search.iso_bound("2026-07-27T15:45:02"), "2026-07-27T15:45:02")
        self.assertEqual(search.iso_bound("2026-07-27"), "2026-07-27")
        self.assertEqual(search.iso_bound("15:45:02"), "15:45:02")
        self.assertIsNone(search.iso_bound(None))

    def test_pattern_in_body(self):
        self.assertEqual(found(search.Filter(pattern="^ *reconfigurationwithsync$")), ["[RRC     ]"])
        self.assertEqual(found(search.Filter(pattern="all_level"), include_config_echo=True), ["[CONFIG  ]"])
