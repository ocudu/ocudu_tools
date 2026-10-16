# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import unittest

from viz.filters import FilterError
from viz.trace import RunTrace, filter_trace, group_trace, join_lanes, lane_keys


def lane(lane_id, t_start, t_end, ue=None, rnti=None, label=None, open_=False):
    return {"lane": lane_id, "t_start": t_start, "t_end": t_end, "ue": ue, "rnti": rnti, "label": label, "open": open_}


def event(t, lane_id, category="rrc", ev_type="x"):
    return {"t": t, "record": int(t * 10), "type": ev_type, "category": category, "lane": lane_id, "ue": None, "rnti": None}


class FakeStore:
    """Store with fixed lanes and events, filtered by time window like Store.trace()."""

    def __init__(self, lanes, events, links=None):
        self._lanes, self._events, self._links = lanes, events, links or {}

    def lane_links(self):
        return self._links

    def events(self, types=None, limit=None):
        return {"events": [e for e in self._events if types is None or e["type"] in types]}

    def trace(self, t0, t1, max_lanes, limit):
        lo = -float("inf") if t0 is None else t0
        hi = float("inf") if t1 is None else t1
        events = [e for e in self._events if lo <= e["t"] <= hi]
        return {"lanes": self._lanes[:max_lanes], "events": events[:limit]}


# F1AP UE contexts: a UE attaching at 10 s, handed over at 20 s to a new context with C-RNTI 0x4602.
F1AP = FakeStore(
    [lane(0, 10.0, 20.5, rnti="0x4601", label="du_f1ap=0 cu_f1ap=0"), lane(1, 20.0, 30.0, rnti="0x4602", label="du_f1ap=1 cu_f1ap=1")],
    [event(9.0, None, "f1ap", "F1Setup"), event(10.0, 0), event(20.0, 1, "f1ap", "UEContextSetup")],
)
# Log lanes: the random access and creation of the first UE, the handover target created without RNTI, its
# contention-free random access with the RNTI only, and a UE of no F1AP context.
LOG = FakeStore(
    [
        lane(0, 9.9, 20.5, ue=0, rnti="0x4601"),
        lane(1, 20.001, 30.0, ue=1),
        lane(2, 20.1, 20.1, rnti="0x4602"),
        lane(3, 50.0, 51.0, ue=5, rnti="0x4609"),
    ],
    [event(9.9, 0, "ra", "prach"), event(20.001, 1, "lifecycle", "ue_create"), event(20.1, 2, "ra", "prach"),
     event(50.0, 3, "ra", "prach"), event(60.0, None, "warning", "warning")],
)


class JoinTest(unittest.TestCase):
    def test_join_lanes(self):
        lanes = join_lanes((7, F1AP), [(3, LOG)])
        self.assertEqual([(lane.key, lane.parts) for lane in lanes], [
            ("7:0", [(7, 0), (3, 0)]),
            ("7:1", [(7, 1), (3, 1), (3, 2)]),
            ("3:3", [(3, 3)]),
        ])
        first, target, unjoined = lanes
        # The log lanes widen their context, e.g. with the random access before the first F1AP message.
        self.assertEqual((first.t_start, first.first("du_ue")), (9.9, "0"))
        self.assertEqual(target.ids, {"du_f1ap": ["1"], "cu_f1ap": ["1"], "rnti": ["0x4602"], "du_ue": ["1"]})
        self.assertEqual(unjoined.ids, {"du_ue": ["5"], "rnti": ["0x4609"]})

    def test_failed_creation_joins_no_ue(self):
        # A creation of DU UE index 7 failing on the C-RNTI of the first UE, while that UE is alive.
        log = FakeStore(
            [lane(0, 9.9, 20.5, ue=0, rnti="0x4601"), lane(1, 15.0, 15.1, ue=7, rnti="0x4601"), lane(2, 16.0, 30.0, ue=7, rnti="0x4602")],
            [event(9.9, 0, "ra", "prach"), event(15.0, 1, "lifecycle", "ue_create"), event(15.1, 1, "failure", "ue_create_failed"),
             event(16.0, 2, "lifecycle", "ue_create")],
        )
        lanes = {lane.key: lane for lane in join_lanes((7, F1AP), [(3, log)])}
        self.assertEqual(lanes["7:0"].ids["du_ue"], ["0"])
        self.assertEqual((lanes["3:1"].parts, lanes["3:1"].open, lanes["3:1"].ids), ([(3, 1)], False, {"du_ue": ["7"], "rnti": ["0x4601"]}))
        trace = RunTrace((7, F1AP), [(3, log)])
        self.assertEqual({lane["lane"]: lane["failed"] for lane in trace.trace(None, None, 300, 100)["lanes"]},
                         {"7:0": False, "7:1": False, "3:1": True})
        by_ue = trace.trace(None, None, 300, 100, group_by="du_ue")
        self.assertEqual([lane["lane"] for lane in by_ue["lanes"]], ["du_ue=0", "3:1", "du_ue=7"])
        # Also in the trace of a log alone, whose lanes do not say which failed.
        alone = group_trace(log.trace(None, None, 300, 100), "du_ue", 300, 100)
        self.assertEqual([lane["lane"] for lane in alone["lanes"]], ["du_ue=0", 1, "du_ue=7"])

    def test_trace_lanes_have_the_ids_of_the_run(self):
        trace = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 100, {7})
        self.assertEqual([(lane["lane"], lane["ue"], lane["rnti"], lane["label"]) for lane in trace["lanes"]],
                         [("7:0", 0, "0x4601", "du_f1ap=0 cu_f1ap=0"), ("7:1", 1, "0x4602", "du_f1ap=1 cu_f1ap=1")])
        # The F1AP contexts get the DU UE index of the logs, also without their events.
        self.assertEqual({e["source"] for e in trace["events"]}, {7})
        by_ue = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 100, {7}, group_by="du_ue")
        self.assertEqual([lane["label"] for lane in by_ue["lanes"]], ["du_ue=0", "du_ue=1"])

    def test_trace(self):
        trace = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 100)
        self.assertEqual(trace["total_lanes"], 3)
        by_type = {e["type"]: (e["source"], e["lane"]) for e in trace["events"]}
        self.assertEqual(by_type["F1Setup"], (7, None))
        self.assertEqual(by_type["ue_create"], (3, "7:1"))
        self.assertEqual(by_type["prach"], (3, "3:3"))
        self.assertEqual(by_type["warning"], (3, None))

    def test_log_rrc_left_out_with_the_f1ap_pcap(self):
        log = FakeStore([lane(0, 9.9, 20.5, ue=0, rnti="0x4601")], [event(10.1, 0, "rrc", "rrc_setup_complete"), event(10.2, 0, "ra", "conres")])
        joined = RunTrace((7, F1AP), [(3, log)])
        self.assertEqual([e["type"] for e in joined.trace(None, None, 300, 100)["events"] if e["source"] == 3], ["conres"])
        log_only = joined.trace(None, None, 300, 100, {3})["events"]
        self.assertEqual([e["type"] for e in log_only], ["rrc_setup_complete", "conres"])

    def test_trace_of_one_source(self):
        trace = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 100, {3})
        # The lanes of the log only, the F1AP contexts it joined and its own.
        self.assertEqual([lane["lane"] for lane in trace["lanes"]], ["7:0", "7:1", "3:3"])
        self.assertEqual({e["source"] for e in trace["events"]}, {3})
        self.assertEqual({e["type"]: e["lane"] for e in trace["events"]}["ue_create"], "7:1")

    def test_filter(self):
        trace = RunTrace((7, F1AP), [(3, LOG)])
        # The F1AP event of lane 7:1 has no RNTI of its own, it takes the one of its lane.
        by_rnti = trace.trace(None, None, 300, 100, filter_expr="rnti == 0x4602")
        self.assertEqual([lane["lane"] for lane in by_rnti["lanes"]], ["7:1"])
        self.assertEqual(sorted(e["type"] for e in by_rnti["events"]), ["UEContextSetup", "prach", "ue_create"])
        by_type = trace.trace(None, None, 300, 100, filter_expr="type == prach or category == warning")
        self.assertEqual([lane["lane"] for lane in by_type["lanes"]], ["7:0", "7:1", "3:3"])
        self.assertEqual([e["type"] for e in by_type["events"]].count("warning"), 1)
        with self.assertRaises(FilterError):
            trace.trace(None, None, 300, 100, filter_expr="rnti ==")

    def test_filter_trace_limits(self):
        full = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 100)
        limited = filter_trace(full, "category == ra", 1, 1)
        self.assertEqual((len(limited["lanes"]), limited["total_lanes"]), (1, 3))
        self.assertEqual((len(limited["events"]), limited["truncated"]), (1, False))

    def test_lane_keys(self):
        self.assertEqual(lane_keys(lane(0, 0, 1, ue=1, rnti="0x4602", label="du_f1ap=1 cu_f1ap=1")),
                         {"du_f1ap": "1", "cu_f1ap": "1", "du_ue": "1", "rnti": "0x4602"})
        self.assertEqual(lane_keys(lane(0, 0, 1)), {})

    def test_group_by_ue(self):
        # The joined lanes carry the DU UE index of the logs: 0 for the first UE, 1 for the handover target.
        trace = RunTrace((7, F1AP), [(3, LOG)])
        by_ue = trace.trace(None, None, 300, 100, group_by="du_ue")
        self.assertEqual([(lane["lane"], lane["label"]) for lane in by_ue["lanes"]], [("du_ue=0", "du_ue=0"), ("du_ue=1", "du_ue=1"), ("du_ue=5", "du_ue=5")])
        self.assertEqual({e["type"]: e["lane"] for e in by_ue["events"]}["UEContextSetup"], "du_ue=1")

    def test_group_merges_lanes_of_the_same_value(self):
        trace = {
            "lanes": [lane(0, 10.0, 12.0, ue=0, label="du_f1ap=0"), lane(1, 20.0, 22.0, ue=0, label="du_f1ap=1"), lane(2, 30.0, 31.0)],
            "events": [event(10.5, 0), event(20.5, 1), event(30.5, 2), event(40.0, None, "warning")],
        }
        by_ue = group_trace(trace, "du_ue", 300, 100)
        self.assertEqual([(lane["lane"], lane["t_start"], lane["t_end"]) for lane in by_ue["lanes"]], [("du_ue=0", 10.0, 22.0), (2, 30.0, 31.0)])
        self.assertEqual([e["lane"] for e in by_ue["events"]], ["du_ue=0", "du_ue=0", 2, None])
        # A row has the identifiers of all the contexts it merges.
        self.assertEqual(by_ue["lanes"][0]["ids"], {"du_f1ap": ["0", "1"], "du_ue": ["0"]})
        # And the span and identifiers of each, which its events point to.
        self.assertEqual([(c["t_start"], c["ids"]) for c in by_ue["lanes"][0]["contexts"]],
                         [(10.0, {"du_f1ap": ["0"], "du_ue": ["0"]}), (20.0, {"du_f1ap": ["1"], "du_ue": ["0"]})])
        self.assertEqual([e["context"] for e in by_ue["events"]], [0, 1, 0, None])
        self.assertEqual(len(group_trace(trace, "du_f1ap", 300, 100)["lanes"]), 3)
        limited = group_trace(trace, "du_ue", 1, 100)
        self.assertEqual((len(limited["lanes"]), limited["total_lanes"], [e["lane"] for e in limited["events"]]), (1, 2, ["du_ue=0", "du_ue=0", None]))

    def test_group_with_filter(self):
        trace = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 100, filter_expr="category == ra", group_by="du_ue")
        self.assertEqual([lane["lane"] for lane in trace["lanes"]], ["du_ue=0", "du_ue=1", "du_ue=5"])
        self.assertTrue(all(e["category"] == "ra" for e in trace["events"]))

    def test_trace_window_and_limits(self):
        trace = RunTrace((7, F1AP), [(3, LOG)]).trace(19.0, 25.0, 1, 100)
        self.assertEqual(([lane["lane"] for lane in trace["lanes"]], trace["total_lanes"]), (["7:0"], 2))
        # Events of lanes beyond max_lanes are left out, like Store.trace().
        self.assertEqual([e["type"] for e in trace["events"]], [])
        trace = RunTrace((7, F1AP), [(3, LOG)]).trace(None, None, 300, 2)
        self.assertEqual((len(trace["events"]), trace["total_events"], trace["truncated"]), (2, 8, True))


if __name__ == "__main__":
    unittest.main()
