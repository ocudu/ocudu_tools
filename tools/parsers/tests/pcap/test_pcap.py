# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parsers.pcap import capture, e1ap, f1ap, frames, messages, ngap, overview, run, timeline, tshark, values
from parsers.pcap.tshark import Tshark, TsharkError, _filter_stderr, split_fields
from parsers.ran.procedures import proc_name

# A single-UE F1AP capture: F1Setup, RRC and NAS attach, and UEContextRelease.
F1AP_PCAP = Path(__file__).resolve().parent / "f1ap_pcap_test.pcap"


class FakeTshark:
    """Tshark returning fixed rows for any extraction."""

    def __init__(self, rows):
        self.rows = rows

    def iter_fields(self, pcap, fields, **kwargs):
        return iter(self.rows)

    def valid_fields(self, pcap, fields):
        return list(fields)


class ValuesTest(unittest.TestCase):
    def test_epoch_to_iso(self):
        self.assertEqual(values.epoch_to_iso("1759326404.3901"), "2025-10-01T13:46:44.390")
        self.assertEqual(values.epoch_to_iso("x"), "x")

    def test_to_int(self):
        self.assertEqual(values.to_int("0x41"), 0x41)
        self.assertEqual(values.to_int("65"), 65)
        self.assertIsNone(values.to_int(""))


class TsharkHelpersTest(unittest.TestCase):
    def test_split_fields(self):
        self.assertEqual(split_fields("a\tb", 3), ["a", "b", ""])
        self.assertEqual(split_fields("a\tb\tc", 2), ["a", "b"])

    def test_filter_stderr(self):
        stderr = (
            "tshark: Error loading table 'User DLTs Table': Permission denied\n"
            "tshark: Could not open your disabled protocols file\n"
            '"/home/u/.config/wireshark/disabled_protos": Permission denied.\n'
            "tshark: Some fields aren't valid:\n"
        )
        self.assertEqual(_filter_stderr(stderr), "tshark: Some fields aren't valid:")
        stderr = (
            'tshark: Can\'t open your preferences file "/home/u/.config/wireshark/preferences": Permission denied.\n'
            'tshark: The file "a.pcap" appears to have been cut short in the middle of a packet.\n'
        )
        self.assertEqual(_filter_stderr(stderr), 'tshark: The file "a.pcap" appears to have been cut short in the middle of a packet.')

    def test_cache_path_depends_on_tag_and_tshark_version(self):
        with tempfile.TemporaryDirectory() as work, mock.patch.object(Tshark, "version", return_value="TShark 4.4.7"):
            ts = Tshark(work)
            self.assertNotEqual(ts.cache_path("a.pcap", "x"), ts.cache_path("a.pcap", "y"))
            self.assertEqual(ts.cache_path("a.pcap", "x").parent, Path(work))
            first = ts.cache_path("a.pcap", "x")
            with mock.patch.object(Tshark, "version", return_value="TShark 4.6.0"):
                self.assertNotEqual(ts.cache_path("a.pcap", "x"), first)

    def test_version_runs_tshark_once(self):
        with mock.patch.object(tshark, "_executable", return_value="/opt/tshark-test"), \
                mock.patch.object(Tshark, "run", return_value=["TShark (Wireshark) 9.9.9."]) as run_mock:
            ts = Tshark("unused")
            self.assertEqual((ts.version(), Tshark("unused").version()), ("TShark (Wireshark) 9.9.9.",) * 2)
            self.assertEqual(run_mock.call_count, 1)
        tshark._versions.pop("/opt/tshark-test", None)

    def test_missing_tshark_fails_before_staging(self):
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as work, \
                mock.patch.object(tshark, "_READABLE_PREFIXES", ()), mock.patch.object(shutil, "which", return_value=None):
            pcap = Path(src_dir) / "f1ap.pcap"
            pcap.write_bytes(b"x")
            ts = Tshark(work)
            with self.assertRaisesRegex(TsharkError, "tshark not found"):
                ts.stage(pcap)
            self.assertFalse((Path(work) / "pcap-stage").exists())

    def test_default_work_dir_is_private(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tempfile, "tempdir", tmp):
            work = tshark.default_work_dir()
            work.mkdir(mode=0o755)
            work.chmod(0o755)
            self.assertEqual(Tshark().work_dir, work)
            self.assertEqual(work.stat().st_mode & 0o777, 0o700)
            # Owned by another user: a new private directory instead.
            with mock.patch.object(tshark, "default_work_dir", return_value=work), \
                    mock.patch.object(os, "getuid", return_value=os.getuid() + 1), \
                    self.assertWarnsRegex(UserWarning, "not a directory of this user"):
                other = Tshark().work_dir
            self.assertNotEqual(other, work)
            self.assertEqual((other.stat().st_uid, other.stat().st_mode & 0o777), (os.getuid(), 0o700))

    def test_stage_follows_a_replaced_pcap(self):
        # No directory read in place, so that the pcap is staged wherever the tree is, as when tshark cannot read it.
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as work, \
                mock.patch.object(tshark, "_READABLE_PREFIXES", ()), mock.patch.object(tshark, "_executable"), \
                mock.patch.object(Tshark, "_reads_in_place", return_value=False):
            ts = Tshark(work)
            pcap = Path(src_dir) / "f1ap.pcap"
            pcap.write_bytes(b"run A, longer")
            first = ts.stage(pcap)
            self.assertEqual((first.read_bytes(), ts.stage(pcap)), (b"run A, longer", first))
            # Run B at the same path, with an older mtime, as an extracted archive keeps.
            pcap.unlink()
            pcap.write_bytes(b"run B")
            os.utime(pcap, ns=(0, 0))
            self.assertEqual(ts.stage(pcap).read_bytes(), b"run B")
            # Only the current version stays staged, with no temp files.
            self.assertEqual([p.read_bytes() for p in (Path(work) / "pcap-stage").iterdir()], [b"run B"])


class ProcNameTest(unittest.TestCase):
    def test_absent_and_reserved_codes(self):
        for code in (None, ""):
            self.assertEqual((proc_name("f1ap", code), proc_name("f1ap", code, with_code=False)), ("?", "?"))
        # TS 38.473 reserves F1AP code 9.
        self.assertEqual(proc_name("f1ap", 9), "9")
        self.assertEqual(proc_name("f1ap", 10), "UEContextReleaseRequest(10)")


class F1apDecodeTest(unittest.TestCase):
    def row(self, code, srbid=None, container="", elements=None):
        return {"code": code, "srbid": srbid, "rrc_container": container, "rrc_elements": elements or {}}

    def test_resolve_rrc(self):
        self.assertEqual(f1ap.resolve_rrc(self.row("12", container="0102", elements={"rrcSetup": "", "rrcRelease": "1"})),
                         "rrcRelease")
        self.assertIsNone(f1ap.resolve_rrc(self.row("12")))
        self.assertEqual(f1ap.resolve_rrc(self.row("11", container="00")), "rrcSetupRequest")
        self.assertEqual(f1ap.resolve_rrc(self.row("6", srbid="0", container="00")), "rrcReject")
        self.assertEqual(f1ap.resolve_rrc(self.row("6", srbid="1", container="0102")), "rrcRelease?")
        self.assertIsNone(f1ap.resolve_rrc(self.row("1", container="00")))

    def test_rrc_outside_an_rrc_container_is_not_carried(self):
        # The UEContextSetupRequest of a handover target, whose HandoverPreparationInformation holds the source's
        # rrcReconfiguration.
        self.assertIsNone(f1ap.resolve_rrc(self.row("5", elements={"rrcReconfiguration": "1"})))
        fields = f1ap.rrc_fields(["rrcReconfiguration"])
        row = dict.fromkeys(fields, "")
        row.update({"f1ap.procedureCode": "5", "nr-rrc.rrcReconfiguration_element": "1"})
        self.assertIsNone(f1ap.carried(row, ["rrcReconfiguration"])["rrc"])

    def test_rrc_container_beside_a_handover_preparation(self):
        # tshark lists one rrcReconfiguration of the source's RRCReconfiguration, of its criticalExtensions, and two of
        # a container's message, of its c1 and its criticalExtensions.
        types = ["rrcReconfiguration", "securityModeCommand"]
        row = dict.fromkeys(f1ap.rrc_fields(types), "")
        row.update({"f1ap.procedureCode": "5", "f1ap.SRBID": "1", "f1ap.RRCContainer": "0102",
                    "f1ap.HandoverPreparationInformation": "08", "nr-rrc.rrcReconfiguration_element": "1",
                    "nr-rrc.securityModeCommand_element": "1,1"})
        self.assertEqual(f1ap.carried(row, types)["rrc"], "securityModeCommand")
        row.update({"nr-rrc.rrcReconfiguration_element": "1,1,1", "nr-rrc.securityModeCommand_element": ""})
        self.assertEqual(f1ap.carried(row, types)["rrc"], "rrcReconfiguration")
        # Without one, a single rrcReconfiguration is the container's.
        row.update({"f1ap.HandoverPreparationInformation": "", "nr-rrc.rrcReconfiguration_element": "1"})
        self.assertEqual(f1ap.carried(row, types)["rrc"], "rrcReconfiguration")


class UeIdsTest(unittest.TestCase):
    def test_ngap_ue_ids_attach_the_amf_id(self):
        rows = [
            ["1", "10.0", "21", "", ""],
            ["2", "11.0", "15", "1", ""],
            ["3", "12.0", "14", "1", "7"],
            ["4", "13.0", "15", "2", ""],
        ]
        ues = ngap.ue_ids(FakeTshark(rows), "ngap.pcap")
        self.assertEqual([(u["frame"], u["message"], u["ran_ue_ngap_id"], u["amf_ue_ngap_id"]) for u in ues],
                         [(2, "InitialUEMessage", "1", "7"), (4, "InitialUEMessage", "2", None)])

    def test_ngap_rows_with_only_the_amf_id_join_its_ue(self):
        rows = [
            ["1", "10.0", "15", "1", ""],
            ["2", "11.0", "14", "1", "9"],
            # HandoverRequest from the AMF, before the gNB allocates a RAN UE id.
            ["3", "12.0", "13", "", "9"],
            ["4", "13.0", "13", "2", "9"],
        ]
        ues = ngap.ue_ids(FakeTshark(rows), "ngap.pcap")
        self.assertEqual([(u["ran_ue_ngap_id"], u["amf_ue_ngap_id"]) for u in ues], [("1", "9")])

    def test_ngap_procedures_by_ran_ue_id(self):
        rows = [
            ["1", "10.0", "21", "", "", "", ""],
            ["2", "11.0", "15", "1", "", "", ""],
            # Several values of an id field, of which the first is the UE's own.
            ["3", "12.0", "14", "1,4", "7", "", ""],
        ]
        by_ue = ngap.procedures(FakeTshark(rows), "ngap.pcap")
        self.assertEqual({ue: [m["frame"] for m in msgs] for ue, msgs in by_ue.items()}, {"(no-ran-ue-id)": [1], "1": [2, 3]})

    def test_e1ap_ue_ids_names(self):
        ues = e1ap.ue_ids(FakeTshark([["5", "1.5", "8", "3", "9"]]), "e1ap.pcap")
        self.assertEqual(ues[0]["e1_cp_ue_id"], "3")
        self.assertEqual(ues[0]["e1_up_ue_id"], "9")
        self.assertEqual(ues[0]["message"], "bearerContextSetup")


class FramesWithoutTsharkTest(unittest.TestCase):
    def test_pcap_magic_and_time_span(self):
        self.assertTrue(frames.is_pcap(F1AP_PCAP))
        first, last = frames.time_span(F1AP_PCAP)
        self.assertLess(first, last)
        self.assertEqual(values.epoch_to_iso(first)[:10], "2026-07-15")
        with tempfile.NamedTemporaryFile(suffix=".log") as f:
            f.write(b"2026-07-15T17:07:49.786 [GNB     ] [I] Built in Release mode\n")
            f.flush()
            self.assertFalse(frames.is_pcap(f.name))
            self.assertIsNone(frames.time_span(f.name))


class CauseTest(unittest.TestCase):
    def test_cause_of_its_group(self):
        row = {"ngap.Cause": "3", "ngap.radioNetwork": "", "ngap.protocol": "6"}
        with mock.patch.object(messages, "_cause_names", return_value={"ngap.protocol": {"6": "unspecified"}}):
            self.assertEqual(messages.cause("ngap", row), "protocol:unspecified")
            # A value without a name, e.g. of a newer release than the tshark build, keeps its number.
            self.assertEqual(messages.cause("f1ap", {"f1ap.Cause": "3", "f1ap.misc": "9"}), "misc:9")
            self.assertIsNone(messages.cause("e1ap", {}))

    @mock.patch.object(messages, "_cause_names", return_value={})
    def test_cause_ie_over_item_causes(self, _):
        # An InitialContextSetupFailure with a failed PDU session, whose item cause precedes the Cause IE.
        row = {"ngap.Cause": "3", "ngap.cause": "0", "ngap.radioNetwork": "13", "ngap.protocol": "6"}
        self.assertEqual(messages.cause("ngap", row), "protocol:6")
        # Items of the group of the Cause IE.
        row = {"ngap.Cause": "0", "ngap.cause": "0,0", "ngap.radioNetwork": "13,21,26"}
        self.assertEqual(messages.cause("ngap", row), "radioNetwork:26")

    @mock.patch.object(messages, "_cause_names", return_value={})
    def test_first_item_cause_without_cause_ie(self, _):
        row = {"ngap.Cause": "", "ngap.cause": "3,0", "ngap.radioNetwork": "13", "ngap.protocol": "6"}
        self.assertEqual(messages.cause("ngap", row), "protocol:6")
        self.assertIsNone(messages.cause("e1ap", {"e1ap.Cause": "", "e1ap.cause": "", "e1ap.misc": ""}))


class TimelineTest(unittest.TestCase):
    def test_ue_filter_of_mac_rntis(self):
        mac = {"file": "mac", "ue_ids": ["0x4601"], "summary": "rnti=0x4601 dir=DL"}
        f1 = {"file": "f1ap", "ue_ids": ["17921"], "summary": "procCode=11"}
        self.assertEqual([timeline._has_ue(mac, ue) for ue in ("0x4601", "17921", "17922")], [True, True, False])
        # Protocol ids are matched as they are, not as RNTIs.
        self.assertEqual([timeline._has_ue(f1, ue) for ue in ("17921", "0x4601")], [True, False])

    def test_mac_events_of_common_channels_name_no_ue(self):
        rows = [["1", "1.0", "0x4601", "1", "3"], ["2", "1.1", "0x010b", "1", "2"], ["3", "1.2", "0xffff", "1", "4"]]
        ev = timeline.events(FakeTshark(rows), "mac.pcap", "mac")
        self.assertEqual([e["ue_ids"] for e in ev], [["0x4601"], [], []])
        self.assertEqual([e["frame"] for e in ev if timeline._has_ue(e, "267")], [])


class RunDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve(self):
        (self.dir / "f1ap.pcap").write_bytes(b"x")
        self.assertIn("bail", run.resolve(self.dir))
        self.assertFalse(run.is_run_dir(self.dir))
        (self.dir / "ngap.pcap").write_bytes(b"x")
        self.assertTrue(run.is_run_dir(self.dir))
        res = run.resolve(self.dir)
        self.assertEqual((res["kind"], res["present"]), ("run directory", ["f1ap", "ngap"]))
        res = run.resolve(self.dir / "ngap.pcap")
        self.assertEqual((res["kind"], res["siblings"]), ("single pcap", ["f1ap"]))
        self.assertIn("bail", run.resolve(self.dir / "missing.pcap"))

    def test_prefixed_names(self):
        for name in ("gnb_f1ap.pcap", "gnb_ngap.pcap", "ngap.pcap", "gnb_mac.txt"):
            (self.dir / name).write_bytes(b"x")
        pcaps = run.find_pcaps(self.dir)
        # The unprefixed name wins over a prefixed one.
        self.assertEqual((pcaps["f1ap"].name, pcaps["ngap"].name, pcaps["mac"]), ("gnb_f1ap.pcap", "ngap.pcap", None))
        self.assertTrue(run.is_run_dir(self.dir))
        self.assertEqual([run.pcap_proto(n) for n in ("du-e1ap.pcap", "f1ap.pcapng", "snap.pcap", "f1ap.txt")],
                         ["e1ap", "f1ap", None, None])

    def test_ocudu_pcaps_are_preferred(self):
        for name in ("amarisoft_mac.pcap", "gnb_mac.pcap", "ue-rlc.pcap", "du_rlc.pcapng"):
            (self.dir / name).write_bytes(b"x")
        pcaps = run.find_pcaps(self.dir)
        self.assertEqual((pcaps["mac"].name, pcaps["rlc"].name), ("gnb_mac.pcap", "du_rlc.pcapng"))

    def test_pcaps_of_the_component_dir(self):
        # Retina writes the f1ap.pcap of a split DU in the directory of its component, the others in its run directory.
        run_dir = self.dir / "ocudu-du-1-1" / "2026-09-29_21-31-50"
        run_dir.mkdir(parents=True)
        for path in (run_dir / "rlc.pcap", run_dir / "f1ap.pcap", run_dir.parent / "f1ap.pcap", run_dir.parent / "mac.pcap"):
            path.write_bytes(b"x")
        pcaps = run.find_pcaps(run_dir)
        # The run directory's own pcap first.
        self.assertEqual((pcaps["rlc"], pcaps["f1ap"], pcaps["mac"]),
                         (run_dir / "rlc.pcap", run_dir / "f1ap.pcap", run_dir.parent / "mac.pcap"))
        (run_dir / "f1ap.pcap").unlink()
        self.assertEqual(run.find_pcaps(run_dir)["f1ap"], run_dir.parent / "f1ap.pcap")
        self.assertEqual(run.resolve(run_dir / "rlc.pcap")["siblings"], ["mac", "f1ap"])
        # Only a run directory, by its name, has pcaps in its parent.
        other = self.dir / "ocudu-du-1-1" / "pcaps"
        other.mkdir()
        self.assertEqual(run.find_pcaps(other), dict.fromkeys(run.PROTOCOLS))

    def test_pcaps_of_a_parent_that_is_no_component_dir(self):
        # A Retina run directory downloaded next to the pcap of another run.
        run_dir = self.dir / "2026-09-29_21-31-50"
        run_dir.mkdir()
        (run_dir / "mac.pcap").write_bytes(b"x")
        (self.dir / "gnb_ngap.pcap").write_bytes(b"x")
        self.assertEqual(run.find_pcaps(run_dir), {**dict.fromkeys(run.PROTOCOLS), "mac": run_dir / "mac.pcap"})

    def test_relative_paths_of_the_component_dir(self):
        run_dir = self.dir / "ocudu-gnb-1" / "2026-09-29_21-31-50"
        run_dir.mkdir(parents=True)
        (run_dir / "mac.pcap").write_bytes(b"x")
        (run_dir.parent / "ngap.pcap").write_bytes(b"x")
        cwd = os.getcwd()
        os.chdir(run_dir)
        try:
            pcaps = run.find_pcaps(".")
        finally:
            os.chdir(cwd)
        self.assertEqual((pcaps["mac"], pcaps["ngap"]), (Path("mac.pcap"), Path("../ngap.pcap")))

    def test_check_empty_pcap(self):
        (self.dir / "mac.pcap").write_bytes(b"")
        res = run.check_pcap(Tshark(self.dir / "work"), self.dir / "mac.pcap")
        self.assertEqual((res["ok"], res["reason"]), (False, "empty (0 bytes)"))


@unittest.skipUnless(shutil.which("tshark"), "tshark not installed")
class F1apPcapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_events = []
        self.tshark = Tshark(self.tmp.name, on_cache=lambda path, hit: self.cache_events.append(hit))

    def tearDown(self):
        self.tmp.cleanup()

    def test_f1ap_messages(self):
        records = f1ap.messages(self.tshark, F1AP_PCAP)
        self.assertEqual(len(records), 32)
        self.assertEqual(records[0]["epoch"], frames.summaries(self.tshark, F1AP_PCAP)[0]["epoch"])
        self.assertEqual(records[0]["message"], "F1Setup")
        setup_req = next(r for r in records if r["rrc"] == "rrcSetupRequest")
        self.assertEqual((setup_req["message"], setup_req["crnti"]), ("InitialULRRCMessageTransfer", "17921"))
        self.assertTrue(any(r["nas"] == "RegistrationRequest" for r in records))

    def test_ue_ids_are_cached(self):
        first = f1ap.ue_ids(self.tshark, F1AP_PCAP)
        self.assertEqual(f1ap.ue_ids(self.tshark, F1AP_PCAP), first)
        self.assertEqual(self.cache_events, [False, True])
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["cu_ue_f1ap_id"], first[0]["crntis"]), ("0", ["17921"]))

    def test_cache_of_a_replaced_pcap_is_not_used(self):
        pcap = Path(self.tmp.name) / "f1ap.pcap"
        shutil.copy(F1AP_PCAP, pcap)
        self.assertEqual(len(frames.summaries(self.tshark, pcap)), 32)
        # The same path, now holding only the first frames.
        first = Path(self.tmp.name) / "first.pcap"
        self.tshark.run(["-r", str(pcap), "-c", "3", "-w", str(first), "-F", "pcap"])
        first.replace(pcap)
        self.assertEqual(len(frames.summaries(self.tshark, pcap)), 3)
        self.assertEqual(self.cache_events, [False, False])

    def test_pcap_cut_short(self):
        pcap = Path(self.tmp.name) / "f1ap.pcap"
        data = F1AP_PCAP.read_bytes()
        pcap.write_bytes(data[:-20])
        # The time span ends at the last complete frame, as tshark reads it.
        self.assertEqual(frames.time_span(pcap)[1], frames.summaries(self.tshark, F1AP_PCAP)[-2]["epoch"])
        # Each read warns, the partial records not being cached.
        for _ in range(2):
            with self.assertWarnsRegex(UserWarning, "cut short"):
                self.assertEqual(len(frames.summaries(self.tshark, pcap)), 31)

    def test_pcap_without_frames(self):
        # Only the header, of a process that wrote no frame: the protocol of its name.
        pcap = Path(self.tmp.name) / "gnb_f1ap.pcap"
        pcap.write_bytes(F1AP_PCAP.read_bytes()[:24])
        cap = capture.read(self.tshark, pcap)
        self.assertEqual((cap.proto, cap.frames, cap.messages), ("f1ap", [], []))

    def test_check_pcap(self):
        res = run.check_pcap(self.tshark, F1AP_PCAP)
        self.assertTrue(res["ok"], res)
        self.assertTrue(run.check_pcap(self.tshark, str(F1AP_PCAP))["ok"])
        self.assertEqual(res["dissector"], "f1ap")

    def test_overview(self):
        # The protocol of a pcap is that of its file name.
        pcap = Path(self.tmp.name) / "gnb_f1ap.pcap"
        shutil.copy(F1AP_PCAP, pcap)
        summary = overview.summarise(self.tshark, pcap, top=3)
        self.assertEqual((summary["proto"], summary["packets"], summary["failures"]), ("f1ap", 32, 0))
        self.assertEqual(len(summary["top_procedures"]), 3)
        self.assertEqual(summary["distinct_ues_by_label"]["cu"], ["0"])
        counts = overview.proc_code_counts(self.tshark, F1AP_PCAP, "f1ap", initiating_only=True)
        self.assertEqual(counts["1"], 1)

    def test_summaries_and_decode(self):
        summaries = frames.summaries(self.tshark, F1AP_PCAP)
        self.assertEqual(len(summaries), 32)
        self.assertEqual((summaries[2]["frame"], summaries[2]["protocol"]), (3, "F1AP/NR RRC"))
        self.assertIn("RRC Setup Request", summaries[2]["info"])
        self.assertEqual((summaries[0]["epoch"], summaries[-1]["epoch"]), frames.time_span(F1AP_PCAP))
        decoded = frames.decode(self.tshark, F1AP_PCAP, 3)
        self.assertIn("F1 Application Protocol (InitialULRRCMessageTransfer)", decoded)
        self.assertNotIn("Frame 4:", decoded)

    def test_messages(self):
        msgs = messages.messages(self.tshark, F1AP_PCAP, "f1ap")
        self.assertEqual(len(msgs), 32)
        setup = msgs[0]
        self.assertEqual((setup["procedure"], setup["outcome"], setup["du_ue_f1ap_id"]), ("F1Setup", "initiating", None))
        self.assertEqual(msgs[1]["outcome"], "successful")
        release = [m for m in msgs if m["procedure"] == "UEContextRelease"]
        self.assertEqual([m["outcome"] for m in release], ["initiating", "successful"])
        self.assertEqual((release[0]["du_ue_f1ap_id"], release[0]["cu_ue_f1ap_id"]), ("0", "0"))

    def test_capture_matches_the_separate_reads(self):
        cap = capture.read(self.tshark, F1AP_PCAP)
        self.assertEqual((cap.proto, cap.frame_protocols.split(":")[1]), ("f1ap", "f1ap"))
        self.assertEqual(cap.frames, frames.summaries(self.tshark, F1AP_PCAP))
        detail = {m["frame"]: m for m in f1ap.messages(self.tshark, F1AP_PCAP)}
        expected = [
            {**m, "rrc": detail[m["frame"]]["rrc"], "nas": detail[m["frame"]]["nas"], "crnti": detail[m["frame"]]["crnti"]}
            for m in messages.messages(self.tshark, F1AP_PCAP, "f1ap")
        ]
        self.assertEqual([{k: v for k, v in m.items() if k != "links"} for m in cap.messages], expected)
        self.assertEqual(cap.pdus, [])
        # The NAS PDUs of the attach, which NGAP carries too, link the context to its NGAP one.
        self.assertTrue(any("nas" in m["links"] for m in cap.messages))

    def test_cause_and_nas_fields_unknown_to_tshark_are_skipped(self):
        expected = (messages.messages(self.tshark, F1AP_PCAP, "f1ap"), f1ap.messages(self.tshark, F1AP_PCAP),
                    capture.read(self.tshark, F1AP_PCAP).messages)
        causes = messages.cause_fields
        with mock.patch.object(messages, "cause_fields", lambda proto: [*causes(proto), "f1ap.unknownCause"]), \
                mock.patch.object(f1ap, "NAS_FIELDS", [*f1ap.NAS_FIELDS, "nas_5gs.unknown_type"]):
            tshark = Tshark(self.tmp.name, cache=False)
            got = (messages.messages(tshark, F1AP_PCAP, "f1ap"), f1ap.messages(tshark, F1AP_PCAP),
                   capture.read(tshark, F1AP_PCAP).messages)
        self.assertEqual(got, expected)

    def test_without_cache(self):
        tshark = Tshark(self.tmp.name, cache=False)
        self.assertEqual(len(f1ap.ue_ids(tshark, F1AP_PCAP)), 1)
        self.assertEqual(list(Path(self.tmp.name).glob("pcap-cache-*")), [])

    def test_events(self):
        events = timeline.events(self.tshark, F1AP_PCAP, "f1ap")
        self.assertEqual(len(events), 32)
        self.assertEqual(events[0]["summary"], "procCode=1")
        self.assertTrue(any(e["ue_ids"] for e in events))


if __name__ == "__main__":
    unittest.main()
