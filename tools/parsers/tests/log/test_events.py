# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import unittest
from datetime import datetime

from parsers.log import events, preamble

# Real log lines, from reestablishment, handover, attach and two-step RACH test logs.
LINES = {
    "prach": "2026-07-30T21:09:59.558753 [SCHED   ] [I] [    34.6] Processed slot events pci=1: prach(ra-rnti=0x10b preamble=23 tc-rnti=0x4602), prach(ra-rnti=0x10b preamble=24 tc-rnti=0x4603)",
    "prach_ssb": "2026-08-10T10:00:00.000000 [SCHED   ] [I] [    34.6] Processed slot events pci=1: RACH Ind slot_rx=34.5, prach(msgb-rnti=0x470b preamble=63 ssb=0 tc-rnti=0x4601)",
    "prach_debug": "2026-08-07T08:46:30.733373 [SCHED   ] [D] [    49.6] Processed slot events pci=1:",
    "msg3": "2026-07-30T21:09:59.556835 [MAC     ] [I] [    34.1] UL rnti=0x4601 subPDUs: [CCCH48: len=6, PAD: len=3]",
    "conres": "2026-07-30T21:09:59.557939 [MAC     ] [I] [    34.5] DL PDU: ue=0 rnti=0x4601 size=9: CON_RES: id=1c51f3668666",
    "ue_create": '2026-07-30T20:53:14.482176 [DU-MNG  ] [I] ue=0 rnti=0x4601 proc="UE Create": Procedure started....',
    "ue_create_no_rnti": '2026-07-30T20:53:20.100000 [DU-MNG  ] [I] ue=3 proc="UE Create": Procedure started....',
    "ue_config": '2026-07-30T20:53:20.100100 [DU-MNG  ] [I] ue=3 rnti=0x4605 proc="UE Configuration": Procedure started....',
    "ue_create_failed": '2026-04-09T21:20:28.714986 [DU-MNG  ] [W] ue=865 rnti=0x1 proc="UE Create": Procedure failed. Cause: Failed to create DU UE context. Cause: UE already existed with same ue_index or C-RNTI.',
    "ue_delete": '2026-07-30T20:53:25.816892 [DU-MNG  ] [I] ue=0 proc="UE Delete": Procedure finished successfully.',
    "rrc_setup_complete": "2026-07-30T20:53:14.500000 [RRC     ] [I] ue=0 c-rnti=0x4601: DCCH UL rrcSetupComplete",
    "rrc_release": "2026-07-30T20:53:40.000000 [RRC     ] [I] ue=12 c-rnti=0x460f: DCCH DL rrcRelease",
    "rrc_reest_request": "2026-07-30T21:10:01.000000 [RRC     ] [I] ue=10 c-rnti=0x460d: CCCH UL rrcReestablishmentRequest",
    "rrc_setup_complete_debug": "2026-07-30T20:53:14.500000 [RRC     ] [D] ue=0 c-rnti=0x4601: Rx SRB1 DCCH UL rrcSetupComplete (96 B)",
    "rrc_containerized": "2026-07-30T20:53:14.500010 [RRC     ] [D] ue=0 c-rnti=0x4601: Containerized rrcSetupComplete: [",
    "ho_trigger": "2026-07-30T20:53:30.000000 [CU-CP   ] [I] ue=0: Trigger intra-CU (inter-DU) handover from source_du=1 to target_du=0",
    "ho_preparation": "2026-07-30T21:01:50.000000 [NGAP    ] [I] ue=3 ran_ue=3 amf_ue=109: Starting HO preparation",
    "rlf_mac": "2026-07-30T21:10:05.000000 [MAC     ] [I] [   268.6] ue=11: RLF detected. Cause: 100 consecutive HARQ-ACK KOs",
    "rlf_du": '2026-07-30T21:10:05.100000 [DU-MNG  ] [W] ue=11 rnti=0x460f: RLF detected with cause "RLC max ReTxs reached". Timer of 1000 msec to release UE started...',
    "reest_failed": '2026-07-30T21:10:09.000000 [RRC     ] [W] ue=12 c-rnti=0x460f: "RRC Reestablishment Procedure" for old_ue=3 failed. Cause: timed out after 4000ms',
    "reest_rejected": "2026-07-30T21:10:10.000000 [RRC     ] [I] ue=18 c-rnti=0x4617: Rejecting RRC Reestablishment to old UE c-rnti=0x4611, pci=1. Cause: Old UE bearers were not fully established. Fallback to RRC Setup Procedure...",
    "conres_unacked": "2026-10-05T20:09:22.471074 [SCHED   ] [I] ue=202 rnti=0x46d5: ra-ContentionResolutionTimer (64ms) expired, but the scheduler never got back a positive ACK. The scheduler will stop retransmitting the ConRes CE",
    "reest_failed_no_cause": '2026-07-30T21:10:09.500000 [RRC     ] [I] ue=13 c-rnti=0x4610: "RRC Reestablishment Procedure" for old_ue=4 failed. Requesting UE context release',
    "warning_ra_rnti": "2026-10-05T20:11:10.300000 [MAC     ] [W] [  468.13] ra-rnti=0x10b: Discarding RAR. Cause: no PDCCH",
    "conres_timeout": "2026-10-05T20:09:22.471074 [SCHED   ] [W] [   959.6] ue=201 rnti=0x46d4: ra-ContentionResolutionTimer, for prach/msg3 rx at slot=952.18, expired (64ms) before ConRes CE was scheduled. UE will stop being scheduled",
    "rrc_setup_timeout": '2026-10-05T20:09:24.415087 [RRC     ] [W] ue=201 c-rnti=0x46d4: "RRC Setup Procedure" timed out after 2000ms',
    "warning_rnti": "2026-10-05T20:11:10.204373 [MAC     ] [W] [  468.12] UL rnti=0x5ec8 lcid=0x3a CE: Discarding PDU. Cause: C-RNTI in C-RNTI CE is not associated with any existing UE.",
    "warning": "2026-07-30T21:10:11.000000 [SCHED   ] [W] UE creation (ue=4): latency1=500us",
    "error": "2026-07-30T21:10:12.000000 [PHY     ] [E] [   300.1] Something failed",
    "noise": "2026-07-30T21:10:13.000000 [RRC     ] [I] ue=0 c-rnti=0x4601: DCCH DL rrcReconfiguration",
    "wrong_layer": '2026-07-30T21:10:14.000000 [MAC     ] [I] ue=0 rnti=0x4601 proc="UE Create": Procedure started....',
}


# Continuation lines of the "prach_debug" entry.
PRACH_DEBUG_BODY = [
    "- RACH ind: slot_rx=48.19 PRACHs: preamble=63 ta=0us",
    "- PRACH: slot=48.19 preamble=63 msgb-rnti=0x470b temp_crnti=0x4601 ta_cmd=0",
    "- PRACH: slot=48.19 preamble=12 ra-rnti=0x10b temp_crnti=0x4602 ta_cmd=3",
    # Of newer builds, with the SSB, and of older ones, with the PCI.
    "- PRACH: slot=48.19 preamble=7 ssb=0 ra-rnti=0x10b temp_crnti=0x4603 ta_cmd=3",
    "- PRACH: slot=48.19, pci=1 preamble=8 ra-rnti=0x10b temp_crnti=0x4604 ta_cmd=3",
]


def parse_all(key, body=()):
    line = LINES[key]
    return events.parse(line, body) if events.is_candidate(line) else []


def parse(key):
    found = parse_all(key)
    return found[0] if found else None


class EventsTest(unittest.TestCase):
    def test_random_access(self):
        self.assertEqual([(parse(k)["type"], parse(k)["category"], parse(k)["rnti"]) for k in ("msg3", "conres")],
                         [("msg3", "ra", "0x4601"), ("conres", "ra", "0x4601")])
        self.assertEqual(parse("conres")["ue"], 0)

    def test_prach_info_one_event_per_preamble(self):
        found = parse_all("prach")
        self.assertEqual([(e["type"], e["category"], e["rnti"], e["level"]) for e in found],
                         [("prach", "ra", "0x4602", "I"), ("prach", "ra", "0x4603", "I")])
        self.assertEqual(found[1]["text"], "prach(ra-rnti=0x10b preamble=24 tc-rnti=0x4603)")
        self.assertEqual([e["rnti"] for e in parse_all("prach_ssb")], ["0x4601"])

    def test_prach_debug_in_continuation_lines(self):
        line = LINES["prach_debug"]
        self.assertTrue(events.has_body(preamble.match_preamble(line)))
        self.assertFalse(events.has_body(preamble.match_preamble(LINES["prach"])))
        found = parse_all("prach_debug", PRACH_DEBUG_BODY)
        self.assertEqual([(e["type"], e["rnti"], e["level"]) for e in found],
                         [("prach", "0x4601", "D"), ("prach", "0x4602", "D"), ("prach", "0x4603", "D"), ("prach", "0x4604", "D")])
        self.assertEqual(found[0]["text"], "PRACH: slot=48.19 preamble=63 msgb-rnti=0x470b temp_crnti=0x4601 ta_cmd=0")
        self.assertEqual(parse_all("prach_debug"), [])
        # Older builds print no pci in the header.
        old = LINES["prach_debug"].replace(" pci=1:", ":")
        self.assertEqual(len(events.parse(old, PRACH_DEBUG_BODY)), 4)

    def test_patterns_bound_to_level(self):
        info_as_debug = LINES["prach"].replace("[I]", "[D]")
        self.assertEqual(events.parse(info_as_debug), [])
        self.assertEqual(events.parse(LINES["prach_debug"].replace("[D]", "[I]"), PRACH_DEBUG_BODY), [])

    def test_lines_of_a_garbled_timestamp_are_skipped(self):
        # A line cut short, with the next one written after it.
        line = "2026-07-30T21:10:12.0000002026-07-30T21:10:12.100000 [PHY     ] [E] [   300.1] Something failed"
        self.assertEqual(events.parse(line), [])

    def test_iter_events(self):
        log = [
            LINES["noise"] + "\n",
            LINES["prach_debug"] + "\n",
            *(b + "\n" for b in PRACH_DEBUG_BODY),
            LINES["msg3"] + "\n",
            LINES["prach_debug"] + "\n",
            PRACH_DEBUG_BODY[1] + "\n",
        ]
        self.assertEqual([(n, e["type"], e["rnti"]) for n, e in events.iter_events(log)],
                         [(2, "prach", "0x4601"), (2, "prach", "0x4602"), (2, "prach", "0x4603"), (2, "prach", "0x4604"),
                          (8, "msg3", "0x4601"), (9, "prach", "0x4601")])

    def test_lifecycle(self):
        e = parse("ue_create")
        self.assertEqual((e["type"], e["category"], e["layer"], e["ue"], e["rnti"]), ("ue_create", "lifecycle", "DU-MNG", 0, "0x4601"))
        self.assertEqual(e["timestamp"].isoformat(), "2026-07-30T20:53:14.482176")
        self.assertIsNone(parse("ue_create_no_rnti")["rnti"])
        self.assertEqual((parse("ue_delete")["type"], parse("ue_delete")["ue"]), ("ue_delete", 0))

    def test_rrc(self):
        self.assertEqual(parse("rrc_setup_complete")["type"], "rrc_setup_complete")
        self.assertEqual(parse("rrc_release")["rnti"], "0x460f")
        self.assertEqual(parse("rrc_reest_request")["category"], "rrc")
        # At debug, with the direction, SRB and size, and the JSON of the message after, which is no event.
        self.assertEqual((parse("rrc_setup_complete_debug")["type"], parse("rrc_setup_complete_debug")["rnti"]),
                         ("rrc_setup_complete", "0x4601"))
        self.assertIsNone(parse("rrc_containerized"))
        old = events.parse("2024-04-22T16:04:26.563020 [RRC     ] [D] ue=1 c-rnti=0x4602: Rx CCCH UL rrcReestablishmentRequest (6 B)")
        self.assertEqual([e["type"] for e in old], ["rrc_reest_request"])

    def test_mobility(self):
        self.assertEqual((parse("ho_trigger")["type"], parse("ho_trigger")["ue"]), ("ho_trigger", 0))
        self.assertEqual(parse("ho_preparation")["type"], "ho_preparation")

    def test_failures_with_cause(self):
        # The DU manager logs every RLF, those that the MAC detects too, so those of the MAC are no events of their own.
        self.assertIsNone(parse("rlf_mac"))
        du = parse("rlf_du")
        self.assertEqual((du["type"], du["rnti"], du["cause"]), ("rlf", "0x460f", "RLC max ReTxs reached"))
        self.assertEqual(parse("reest_failed")["cause"], "timed out after 4000ms")
        no_cause = parse("reest_failed_no_cause")
        self.assertEqual((no_cause["type"], no_cause["ue"], no_cause["cause"]), ("rrc_reest_failed", 13, None))
        self.assertEqual(parse("reest_rejected")["cause"], "Old UE bearers were not fully established")

    def test_ue_create_failed(self):
        rec = parse("ue_create_failed")
        self.assertEqual((rec["type"], rec["category"], rec["ue"], rec["rnti"]), ("ue_create_failed", "failure", 865, "0x1"))
        self.assertEqual(rec["cause"], "Failed to create DU UE context. Cause: UE already existed with same ue_index or C-RNTI")

    def test_attach_timeouts(self):
        conres, setup = parse("conres_timeout"), parse("rrc_setup_timeout")
        self.assertEqual((conres["type"], conres["category"], conres["ue"], conres["rnti"]), ("conres_timeout", "failure", 201, "0x46d4"))
        self.assertEqual((setup["type"], setup["category"], setup["ue"], setup["rnti"]), ("rrc_setup_timeout", "failure", 201, "0x46d4"))
        # The ConRes CE sent but never ACKed, at info.
        # Of older builds.
        old = events.parse("2026-04-09T21:05:55.432032 [SCHED   ] [W] [   799.8] ue=273 rnti=0x7234: ra-ContentionResolutionTimer "
                           "(64ms) expired before ConRes CE was scheduled. UE will stop being scheduled")
        self.assertEqual([e["type"] for e in old], ["conres_timeout"])
        unacked = parse("conres_unacked")
        self.assertEqual((unacked["type"], unacked["category"], unacked["ue"]), ("conres_unacked", "failure", 202))

    def test_generic_warning_ue(self):
        w = parse("warning_rnti")
        self.assertEqual((w["type"], w["ue"], w["rnti"]), ("warning", None, "0x5ec8"))
        self.assertEqual((parse("warning")["ue"], parse("warning")["rnti"]), (4, None))
        # The RA-RNTI of a random access is no UE's.
        self.assertEqual((parse("warning_ra_rnti")["ue"], parse("warning_ra_rnti")["rnti"]), (None, None))

    def test_warning_and_error_levels(self):
        w, e = parse("warning"), parse("error")
        self.assertEqual((w["type"], w["category"], w["text"]), ("warning", "warning", "UE creation (ue=4): latency1=500us"))
        self.assertEqual((e["type"], e["category"]), ("error", "error"))

    def test_specific_pattern_wins_over_level(self):
        self.assertEqual(parse("rlf_du")["type"], "rlf")

    def test_non_events(self):
        self.assertIsNone(parse("noise"))
        self.assertIsNone(parse("wrong_layer"))
        self.assertEqual(events.parse("continuation line without preamble"), [])

    def test_ue_tracker(self):
        def ev(t, type_, layer, ue=None, rnti=None):
            return {"timestamp": datetime(2026, 1, 1, 0, 0, t), "type": type_, "layer": layer, "ue": ue, "rnti": rnti}

        def binding(t, ue, rnti):
            return {**ev(t, "ue_config", "DU-MNG", ue, rnti), "category": events.BINDING}

        tracker = events.UeTracker()
        assigned = [
            tracker.assign(e)
            for e in [
                ev(0, "prach", "SCHED", rnti="0x4601"),
                ev(1, "ue_create", "DU-MNG", ue=0, rnti="0x4601"),
                ev(2, "rrc_setup_complete", "RRC", ue=5, rnti="0x4601"),
                ev(3, "ho_trigger", "CU-CP", ue=5),
                ev(4, "rlf", "MAC", ue=0),
                ev(5, "ue_delete", "DU-MNG", ue=0),
                ev(6, "warning", "SCHED"),
                ev(7, "prach", "SCHED", rnti="0x4601"),
                ev(8, "ue_create", "DU-MNG", ue=1),
                ev(9, "conres", "MAC", ue=1, rnti="0x4602"),
                # A handover target, created without RNTI, gets it from its configuration before its random access.
                ev(10, "ue_create", "DU-MNG", ue=2),
                binding(11, 2, "0x4605"),
                ev(12, "prach", "SCHED", rnti="0x4605"),
                # A configuration of no live context binds nothing.
                binding(13, 7, "0x4609"),
            ]
        ]
        self.assertEqual(assigned, [0, 0, 0, 0, 0, 0, None, 1, 2, 2, 3, 3, 3, None])
        first, retry, created, target = tracker.lanes
        self.assertEqual((target.du_ue, target.rnti), (2, "0x4605"))
        self.assertEqual((first.du_ue, first.rnti, first.created, first.deleted), (0, "0x4601", True, True))
        # The CU-CP UE index of its RRC event, which differs from the DU one.
        self.assertEqual(first.cu_ue, 5)
        self.assertEqual((first.t_end - first.t_start).seconds, 5)
        self.assertEqual((retry.du_ue, retry.created, retry.t_end), (None, False, None))
        self.assertEqual((created.du_ue, created.rnti), (1, "0x4602"))

    def test_ue_tracker_indexes(self):
        def ev(t, type_, layer, ue=None, rnti=None):
            return {"timestamp": datetime(2026, 1, 1, 0, 0, t), "type": type_, "layer": layer, "ue": ue, "rnti": rnti}

        tracker = events.UeTracker()
        assigned = [
            tracker.assign(e)
            for e in [
                ev(0, "ue_create", "DU-MNG", ue=3, rnti="0x4601"),
                # CU-CP UE index 3 of another UE, and the DU UE index 3 of RLC.
                ev(1, "rrc_setup_complete", "RRC", ue=3, rnti="0x4602"),
                ev(2, "warning", "RLC", ue=3),
                # The CU-UP UE index, of no event.
                ev(3, "warning", "GTPU", ue=3),
                # DU UE index 3 reused by another RNTI, its deletion not logged.
                ev(4, "conres_timeout", "SCHED", ue=3, rnti="0x4603"),
                ev(5, "warning", "SCHED", ue=3),
            ]
        ]
        self.assertEqual(assigned, [0, 1, 0, None, 2, 2])
        self.assertEqual([lane.du_ue for lane in tracker.lanes], [None, None, 3])
        # An RNTI of a live context with another DU UE index takes no index of it: index 3 is of no context, so its
        # deletion deletes none.
        tracker.assign(ev(6, "ue_create", "DU-MNG", ue=4, rnti="0x4604"))
        tracker.assign(ev(7, "conres_timeout", "SCHED", ue=3, rnti="0x4604"))
        tracker.assign(ev(8, "ue_delete", "DU-MNG", ue=3))
        self.assertEqual([(lane.rnti, lane.deleted) for lane in tracker.lanes][2:], [("0x4603", False), ("0x4604", False)])

    def test_ue_tracker_failed_creation(self):
        def ev(t, type_, layer, ue=None, rnti=None):
            return {"timestamp": datetime(2026, 1, 1, 0, 0, t), "type": type_, "layer": layer, "ue": ue, "rnti": rnti}

        tracker = events.UeTracker()
        assigned = [
            tracker.assign(e)
            for e in [
                ev(0, "ue_create", "DU-MNG", ue=1, rnti="0x1"),
                ev(1, "ue_create", "DU-MNG", ue=2, rnti="0x2"),
                ev(2, "rrc_setup_complete", "RRC", ue=7, rnti="0x2"),
                # A C-RNTI clash, of a creation that leaves no context.
                ev(3, "ue_create", "DU-MNG", ue=5, rnti="0x1"),
                ev(4, "ue_create_failed", "DU-MNG", ue=5, rnti="0x1"),
                # A UE index clash.
                ev(5, "ue_create", "DU-MNG", ue=2, rnti="0x9"),
                ev(6, "ue_create_failed", "DU-MNG", ue=2, rnti="0x9"),
                # The live UEs keep their RNTI and DU UE index, for DU and CU events.
                ev(7, "rrc_setup_timeout", "RRC", ue=3, rnti="0x1"),
                ev(8, "conres", "MAC", ue=1, rnti="0x1"),
                ev(9, "warning", "SCHED", ue=2),
                ev(10, "rrc_release", "RRC", ue=7),
                ev(11, "ue_delete", "DU-MNG", ue=2),
            ]
        ]
        self.assertEqual(assigned, [0, 1, 1, 2, 2, 3, 3, 0, 0, 1, 1, 1])
        self.assertEqual([(lane.du_ue, lane.created, lane.deleted, lane.failed) for lane in tracker.lanes],
                         [(1, True, False, False), (2, True, True, False), (5, False, False, True), (2, False, False, True)])
        # A failure of no ongoing creation changes nothing.
        self.assertIsNone(tracker.assign(ev(12, "ue_create_failed", "DU-MNG", ue=4, rnti="0x4")))

    def test_categories_cover_patterns(self):
        self.assertTrue({p.category for p in events.EVENT_PATTERNS} <= {*events.CATEGORIES, events.BINDING})

    def test_binding(self):
        rec = parse("ue_config")
        self.assertEqual((rec["type"], rec["category"], rec["ue"], rec["rnti"]), ("ue_config", events.BINDING, 3, "0x4605"))


if __name__ == "__main__":
    unittest.main()
