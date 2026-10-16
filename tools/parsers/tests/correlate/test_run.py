# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import tempfile
import unittest
from pathlib import Path

from parsers.correlate.run import log_contexts
from parsers.correlate.ues import Context, combine

# A UE attaching, and a handover target created without its RNTI, which it gets from its configuration before its
# contention-free random access, and a creation failing on its C-RNTI, of no context.
LOG = """\
2026-07-30T20:53:14.400000 [SCHED   ] [I] [    34.6] Processed slot events pci=1: prach(ra-rnti=0x10b preamble=23 tc-rnti=0x4601)
2026-07-30T20:53:14.482176 [DU-MNG  ] [I] ue=0 rnti=0x4601 proc="UE Create": Procedure started....
2026-07-30T20:53:20.100000 [DU-MNG  ] [I] ue=1 proc="UE Create": Procedure started....
2026-07-30T20:53:20.100100 [DU-MNG  ] [I] ue=1 rnti=0x4602 proc="UE Configuration": Procedure started....
2026-07-30T20:53:20.200000 [SCHED   ] [I] [    50.6] Processed slot events pci=1: prach(ra-rnti=0x10b preamble=63 tc-rnti=0x4602)
2026-07-30T20:53:21.000000 [DU-MNG  ] [I] ue=5 rnti=0x4602 proc="UE Create": Procedure started....
2026-07-30T20:53:21.000100 [DU-MNG  ] [W] ue=5 rnti=0x4602 proc="UE Create": Procedure failed. Cause: UE already existed with same ue_index or C-RNTI.
2026-07-30T20:53:25.816892 [DU-MNG  ] [I] ue=0 proc="UE Delete": Procedure finished successfully.
"""


class LogContextsTest(unittest.TestCase):
    def test_log_contexts(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gnb.log"
            path.write_text(LOG)
            contexts = log_contexts(path)
        self.assertEqual([(c.source, c.lane, c.ids, c.open) for c in contexts], [
            ("gnb.log", 0, {"du_ue": 0, "cu_ue": None, "rnti": "0x4601"}, False),
            ("gnb.log", 1, {"du_ue": 1, "cu_ue": None, "rnti": "0x4602"}, True),
        ])
        # Log timestamps are UTC.
        self.assertEqual(contexts[0].t_start, 1785444794.4)
        f1ap = [Context("f1ap.pcap", 0, 1785444800.15, 1785444810.0, {"rnti": "17922", "du_f1ap": "1"})]
        joined = combine(f1ap, contexts)
        self.assertEqual([ue.ids for ue in joined if ue.key == "f1ap.pcap:0"], [{"rnti": ["0x4602"], "du_f1ap": ["1"], "du_ue": ["1"]}])
