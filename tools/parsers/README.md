# Parsers

Decoders and parsers for OCUDU artifacts (logs, pcaps, traces, etc.).

Standard-library only, so it can be imported by visualization or analysis tools without pulling in their dependencies.

## Layout

| Subpackage    | Artifact                                                      |
|---------------|---------------------------------------------------------------|
| `parsers.log` | The text outputs of OCUDU applications: log line preamble, `METRICS` lines, events, the configuration echo, entry search, UE procedure stage latencies and a log summary; and the console output (`stdout.log`), `metrics.json` and the files of a run directory. |
| `parsers.pcap` | The pcaps of a run (`mac`, `rlc`, `f1ap`, `e1ap`, `ngap`), through `tshark`: UE identifiers, F1AP messages with their RRC and NAS messages, NGAP procedures, overviews and a merged timeline. |
| `parsers.ran` | 3GPP knowledge shared by the artifacts: NGAP, F1AP and E1AP procedure codes, NAS and RRC message types. |
| `parsers.schedtrace` | Scheduler event traces (`schedtrace_cell<N>.bin`): the FlatBuffers cell events of the OCUDU scheduler, per slot its inputs and decisions. |
| `parsers.correlate` | Correlation of the artifacts of a run: the UEs that its logs and pcaps see, each with the identifiers of all of them. |

## Installation

```bash
pip install -e tools/parsers            # core, standard library only
pip install -e "tools/parsers[pandas]"  # adds to_dataframe()
```

## Usage

### Metrics

```python
from parsers.log import metrics

parser = metrics.MetricsParser()        # all layers, or e.g. MetricsParser(["sched", "mac"])
with open("gnb.log") as f:
    for line in f:
        rec = parser.parse(line)
        if rec and rec["layer"] == "sched":
            print(rec["timestamp"], rec["total_dl_brate"], parser.units["sched"]["total_dl_brate"])

df = metrics.to_dataframe(open("gnb.log"), "mac")  # units in df.attrs["units"]
```

`parser.parse_file()` parses a whole log, yielding `(line_no, record)` in file order. It splits the log into ranges
that start at log entries (`parsers.log.chunks`), so that memory does not grow with the log. Given an executor, it
parses them in its workers, which is several times faster on large logs:

```python
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from parsers.log import metrics

if __name__ == "__main__":  # needed by worker processes that start with "spawn" or "forkserver"
    parser = metrics.MetricsParser()
    with ProcessPoolExecutor(mp_context=multiprocessing.get_context("forkserver")) as executor:
        for line_no, rec in parser.parse_file("gnb.log", executor):
            ...
    print(parser.units)  # merged from all ranges once the iteration ends
```

Records are flat dicts with `timestamp`, `layer`, the line context fields (e.g. `pci`) and the metric fields:

- `timestamp` is a `datetime` without a zone, of the UTC wall clock that OCUDU logs: `ts.replace(tzinfo=timezone.utc)`
  or `preamble.to_epoch()` give the epoch that pcap times compare with, while `ts.timestamp()` takes it for local time.
  The same holds for the timestamps of events.
- Units are normalized: times to `us`, bitrates to `bps`, SI-prefixed unitless values (`5.74k`) to plain numbers.
  The unit of each field is in `parser.units[layer][field]`.
  Fields that OCUDU prints without a unit (e.g. `dl_bs` in bytes, `pusch_snr_db` in dB) get the one in
  `metrics.IMPLIED_UNITS`.
- `n/a`, `NaN` and `ovl` become `None`.
- Slot fields (`sfn.slot`) stay strings. Use `metrics.parse_slot()` to split them.
- `[k=v ...]` blocks and `<section>:` groups are flattened as `<block>_<key>`.
- Number lists stay lists, and `{...}` record lists become lists of dicts.

Supported layers (keys of `metrics.LAYER_PATTERNS`), matching the current OCUDU log format:

| Layer            | Log line prefix            |
|------------------|----------------------------|
| `du_manager`     | `DU manager metrics:`      |
| `mac`            | `MAC cell pci=N metrics:`  |
| `sched`          | `Scheduler cell pci=N metrics:` |
| `sched_ue`       | `Scheduler UE ue=N pci=N rnti=X metrics:` |
| `rlc`            | `RLC Metrics:`             |
| `phy`            | `PHY metrics:` (first line only) |
| `ofh_timing`     | `OFH timing metrics:`      |
| `ofh_sector`     | `OFH sector#N metrics:`    |
| `pdcp`           | `PDCP Metrics:`            |
| `nrup`           | `NRUP Metrics:`            |
| `e1ap`           | `CU-UP E1AP metrics:`      |
| `exec`           | `Executor metrics "name":` |
| `resource_usage` | `App resource usage:`      |
| `buffer_pool`    | `Buffer pool:`             |

Unit conflicts and unknown units are reported once per field on the `parsers` logger. The `CU-CP RRC metrics:` and
`CU-CP NGAP metrics:` lines are not parsed: they hold one block per DU, AMF or slice, which flat records cannot keep.

### Events

```python
from parsers.log import events

with open("gnb.log") as f:
    for line_no, ev in events.iter_events(f):
        print(line_no, ev["timestamp"], ev["category"], ev["type"], ev["ue"], ev["rnti"], ev["text"])
```

`events.iter_events()` recognizes the lines of known events (`events.EVENT_PATTERNS`) and reports the other warning and
error lines as generic `warning` and `error` events, with the `ue=` and RNTI (`rnti=`, `c-rnti=`, `tc-rnti=` or
`crnti=`, not `ra-rnti=` or `msgb-rnti=`) of their message if any. `events.UeTracker` takes an all-zero RNTI as none:
the DU F1AP logs one for a UE created without RNTI, e.g. a handover target, as the `c-rnti=0x0` of its DU-F1 lines or
the `crnti=0x0` of its failed F1 UE context creation. Records have `timestamp`, `type`, `category`, `layer`, `level`,
`ue`, `rnti`, `cause` and `text`. Records of category `events.BINDING` are not events: `ue_config` gives
`events.UeTracker` the RNTI of a DU UE created without it, e.g. a handover target, and `cu_ue_new` (CU-UEMNG `Created
new CU-CP UE`) and `cu_ue_create` (CU-CP `ue=N c-rnti=0x..: UE created`) bind a CU-CP UE index to its RNTI for its
lifetime, also for a UE that arrived by handover and logs no RRC setup, so that its CU-CP and NGAP events, e.g. its own
handover, have a context. Without DU lines, e.g. in a CU log, a CU-CP UE created with the RNTI of a context of another
CU-CP UE index, e.g. on another DU, is a new context, and the events of each of these CU-CP UEs stay in its own. A
failed UE creation, e.g. on a UE index or C-RNTI clash, closes its context (`UeLane.failed`) and leaves the UE it
clashed with as it was, also when the creation logged no RNTI and a warning of it did; that context is not open and
joins no UE of `parsers.correlate`.

| Category    | Types                                                              |
|-------------|--------------------------------------------------------------------|
| `ra`        | `prach`, `msg3`, `conres`                                          |
| `lifecycle` | `ue_create`, `ue_delete`                                           |
| `rrc`       | `rrc_setup_complete`, `rrc_release`, `rrc_reest_request`           |
| `mobility`  | `ho_trigger`, `ho_preparation`                                     |
| `failure`   | `ue_create_failed`, `rlf`, `rrc_reest_failed`, `rrc_reest_rejected`, `conres_timeout`, `conres_unacked`, `rrc_setup_timeout` |
| `warning`   | `warning` (other `[W]` lines)                                      |
| `error`     | `error` (other `[E]` lines)                                        |

A log entry can hold several events, and some messages change format with the log level: the scheduler prints its slot
events inline at info level (`prach(...)`, one event per preamble) and one per continuation line at debug level
(`- PRACH: ...`), and the RRC adds the SRB and size of its messages at debug level. Patterns are bound to the level of
the entry where the format differs. An RLF is the one the DU manager logs, which it does for those the MAC detects too.
To read a log line by line without `iter_events()`, pass the continuation lines of the entries for which
`events.has_body()` is true to `events.parse()`.

### Configuration

```python
from parsers.log import config

cfg = config.from_log(open("gnb.log"))  # None if the log has no configuration echo
if cfg and not cfg.logs_at("SCHED", "info"):
    print("no PRACH events: mac_level is", cfg.level("mac"))
```

`config.from_log()` reads the configuration that OCUDU echoes at the top of its logs (`config_level` info or debug), and
`config.parse_config()` the text of a YAML configuration file, e.g. `parse_config(Path("gnb.yml").read_text())`. `AppConfig` has the explicit log levels, the effective level of each
option or logger (`level()`, `logger_level()`, `logs_at()`), and the node planes and type.

### Pcaps

`parsers.pcap` needs `tshark` on the `PATH`.

```python
from parsers.pcap import f1ap, ngap, overview, run, timeline
from parsers.pcap.tshark import Tshark

tshark = Tshark()  # stages pcaps and caches extractions in a work directory, by default under the temp dir
pcaps = run.find_pcaps("run_dir")  # {"mac": Path | None, "rlc": ..., "f1ap": ..., "e1ap": ..., "ngap": ...}
for ue in f1ap.ue_ids(tshark, pcaps["f1ap"]):
    print(ue["first_iso"], ue["cu_ue_f1ap_id"], ue["crntis"])
for msg in f1ap.messages(tshark, pcaps["f1ap"]):
    print(msg["first_iso"], msg["message"], msg["rrc"], msg["nas"])
print(overview.summarise(tshark, pcaps["ngap"]))
events = timeline.run_events(tshark, "run_dir", ["ngap", "f1ap"], ue="0")
```

`tshark` reads pcaps through a link or copy in the work directory, since the AppArmor profile of `tshark` on Ubuntu only
lets it read under `/tmp`, and dissects the MAC and RLC PDUs of `mac.pcap` and `rlc.pcap` through the `mac_nr_udp` and
`rlc_nr_udp` heuristics. A pcap whose last record is cut short, e.g. by a crash, gives its complete records, with a
warning. Field extractions are cached in the work directory by pcap path, size and mtime, and a tag, by default the
fields and filter. `run.find_pcaps()` finds the pcaps of each protocol by file name, `<proto>.pcap` or with a prefix,
e.g. `gnb_f1ap.pcap`, and those missing from a Retina run directory (`YYYY-MM-DD_HH-MM-SS`) in its parent when that is
the directory of an OCUDU component (`ocudu-gnb-*`, `ocudu-du-*`, `ocudu-cu-*`, ...), e.g. the `f1ap.pcap` of a split
DU. The paths are relative when the run directory is. `run.check_pcap()` checks that one of the known dissectors binds
the first frame of a pcap. `messages.messages()` lists the NGAP, F1AP or E1AP messages of a pcap with their procedure,
outcome, UE identifiers and cause, as `<group>:<value>` named by the value tables of tshark, e.g.
`protocol:unspecified`, `frames.summaries()` the one-line tshark summary of each frame and `frames.decode()` decodes one
frame. The `rrc` of an F1AP message is the RRC message of its RRC container, None without one, e.g. for the
`UEContextSetupRequest` of a handover target, whose `HandoverPreparationInformation` holds the source's
`rrcReconfiguration`, which is not counted beside a container. RRC, NAS and cause fields that a `tshark` build does not
know are left out of extractions, so those values are None. `frames.time_span()` reads the time span of a classic pcap
from its complete packets' headers, without `tshark`, and gives None for pcapng. `capture.read()` gives the protocol,
the frame summaries and the messages or PDUs of a pcap in one pass over its frames, after reading its first frame for
the protocol, or its file name for a pcap without frames, unless given one, e.g. for a capture whose first frame is TCP.
`contexts.ContextTracker` assigns the NGAP, F1AP or E1AP messages to UE contexts, by their UE identifiers until the
release of the context, with the C-RNTI of the F1AP ones and the links between contexts, given the messages of
`capture.read()`, which carry them; the messages of `messages.messages()` give contexts without either.
`contexts.ue_trace()` gives the F1AP contexts of a C-RNTI and those that continue them: the targets of their handovers
and the contexts that reestablish them, and so on.

### Scheduler event traces

```python
from parsers import schedtrace

for ev in schedtrace.read("schedtrace_cell0.bin"):
    if ev["type"] == "CellSlotEvent":
        harq = [i for i in ev["inputs"] or [] if i and i["type"] == "HarqAckEvent"]
        pdschs = (ev["decision"] or {}).get("ue_pdschs") or []
        print(ev["timestamp_us"], ev["slot_tx"], ev["latency_us"], len(pdschs), harq)
```

`decision` is `None` in a slot without one, as is an element of `inputs` of union type `NONE`.

OCUDU writes one `schedtrace_cell<N>.bin` per cell when the `trace.schedtrace` option is enabled: an 8-byte header
(magic `SCHT`, then the major and minor version of the file format as little-endian `uint16`) and a series of
size-prefixed FlatBuffers `CellEvent` buffers. `read()` and `events()` yield each as a dict with `timestamp_us`,
`type` (`CellStartEvent`, `CellSlotEvent` or `CellStopEvent`) and its fields: tables as dicts, vectors as lists, enums
by name, unions with their member as `type`, absent optional fields as `None`. An incomplete last event, e.g. of a
trace being written, is left out. A size prefix over `MAX_EVENT_SIZE` (1 MiB) is corrupt: the events from it are left
out, with a warning.

`schedtrace.FORMAT_VERSION` is the version of the schemas of `schedtrace/fbs/`, from their `FileFormatVersionMajor`
and `FileFormatVersionMinor` enums. Traces of another major version, and traces without a header, written before OCUDU versioned the
format, raise `schedtrace.FormatError`; traces of a newer minor version decode with a warning, without the fields they
append. `schedtrace.version()` reads the version in the header of the contents of a trace, its bytes, e.g.
`version(Path("schedtrace_cell0.bin").read_bytes()[:8])`.

The decoder is standard-library only: `schedtrace.schema` reads the FlatBuffers schemas of `schedtrace/fbs/`, copies of
`lib/scheduler/trace/fbs` of OCUDU, and `schedtrace.flatbuf` decodes buffers with them. FlatBuffers' Python code
generator does not support the vectors of unions of the schemas (`CellSlotEvent.inputs`). Decoding uses
`schedtrace/_generated.py`, which `schedtrace.codegen` compiles from the schemas into one function per table and struct,
about 2.3 times faster than `schedtrace.flatbuf`. With `grid_only=True`, `read()`, `events()` and `event_at()` decode
only the fields of `codegen.GRID_FIELDS`, those that `grid.allocations()` and `ue_metrics` read, the timestamp, slot and
latency of an event and the type of each input, 1.6 times faster again. When OCUDU changes the schemas, copy them from
an OCUDU tree, its working tree or a revision, and regenerate it:

```bash
python -m parsers.schedtrace.codegen --ocudu ~/srs/ocudu [--rev origin/dev]
```

`_generated.py` holds the file format version and the hash of the schemas, the generator and the schema parser, and a
test fails when it is stale. The tests compare the decoder with flatc on a trace that `tests/schedtrace/data/make_fixtures.sh` builds, and
the generated decoder with `schedtrace.flatbuf`. It decodes about 18k slots per second of busy slots.

`schedtrace.collisions` finds the allocations of a slot whose resources overlap: PDSCH with PDSCH, PUSCH with PUSCH,
PUSCH with PUCCH, PDCCH with PDSCH, and PDCCH with PDCCH, by their CCEs in a CORESET. PDSCHs are placed as `grid.allocations()` places
them: those of DCI format 1_0 in a common search space (SIB, RAR, paging, fallback), matched to their PDCCH in the slot,
from the lowest CRB of its CORESET (TS 38.214 5.1.2.2.2), the others from the BWP start; PDCCHs from the REG bundles
of their CCEs (TS 38.211 7.3.2.2). `skills/analyze-ran-log/scripts/ocudu/schedtrace_collisions.py` prints them.

`schedtrace.ue_metrics` gives the per-UE metrics of a trace: `slot_items()` the DL and UL grants, HARQ-ACK and CSI
reports and SRs of a slot, per UE, a DL grant with the MCS of its first and second codeword (`mcs`, `mcs2`) and the TBS
of both, and `Aggregator` their sums per UE and 10 ms bin, as rows named after the fields of the METRICS `Scheduler UE`
lines where they mean the same (`dl_brate` from the ACKed TBS, `dl_mcs` as the mean of every codeword, `dl_nof_ok`,
`dl_nof_nok` of the NACK and DTX reports, `dl_error_rate`, `cqi` and `dl_ri` as means of a bin, all means unrounded
(METRICS prints `dl_mcs` and `cqi` rounded half away from zero, as `ue_metrics.round_half_up()` does), `sr_count`), plus
the scheduler latency, PRBs and UEs of the cell per bin. Aggregators of parts of a trace merge; `Aggregator(bin_s=1)`
gives 1 s bins, the length of the default METRICS period, and `ue_totals()` the totals of each UE over a span. Bins
start at whole multiples of `bin_s` since the epoch, while a METRICS period ends at the time of its line, so a 1 s bin
overlaps two METRICS periods. Bins are whole microseconds, at least one: `Aggregator.bin_s` holds the size rounded to
them. `skills/analyze-ran-log/scripts/ocudu/schedtrace_ue_metrics.py` prints them. The trace has no UL CRC or buffer
status: UL rates are of scheduled new transmissions (`ul_sched_brate`), and `ul_layers` is of the PUSCHs, not the RI of
SRS. HARQ processes that time out without a report, or have no feedback, have no trace event, so they count in neither
`dl_nof_ok` nor `dl_nof_nok`, unlike in METRICS.

```python
from parsers import schedtrace
from parsers.schedtrace import grid, ue_metrics

events = schedtrace.read("schedtrace_cell0.bin", grid_only=True)
cell = grid.cell_of(next(events))
agg = ue_metrics.Aggregator()
for record, ev in enumerate(events, 2):
    if ev["type"] == "CellSlotEvent":
        agg.add(record, ev, ue_metrics.slot_items(ev, cell))
for t, record, *values in agg.ue_rows():
    print(t, dict(zip(ue_metrics.UE_FIELDS, values)))
```

### Ad-hoc metrics of logs

`parsers.log.adhoc` scans a byte range of a log for the numeric values of a `key=value` field, e.g. `tbs=` of the UE
PDSCH lines of the slot decisions, optionally in the lines matching a regex and split by the nearest value of another
key before each value in its line. Values with a unit are scaled as METRICS fields are. Ranges of
`chunks.chunk_ranges()` scan independently. Without a key, `scan_lines()` gives the lines matching the regex, with their
text, logger, level, UE index and RNTI, the first token of those the RNTI filter of `search` matches.

```python
import os

from parsers.log import adhoc

spec = adhoc.Spec("tbs", filter="UE PDSCH", split_key="ue")
res = adhoc.scan("gnb.log", 0, os.path.getsize("gnb.log"), 1, spec)
for t, line, offset, value, ue in res["rows"]:
    print(t, line, value, ue)
```

The filter applies to the line of each value, so it finds the values of an entry header only: at debug level, the
scheduler writes the grants of a slot decision on continuation lines, which a filter of its header text misses.

### Log search, summary and UE procedure latencies

```python
from parsers.log import procedures, run, search, summary

log = run.find_log("e2e/log/tests/.../ocudu-gnb-1")      # the log of the latest run under a directory
with open(log, errors="replace") as f:
    for entry in search.search(f, search.Filter(layer="RRC", rnti="4601", after="18:18:30")):
        print(entry)
with open(log, errors="replace") as f:
    s = summary.summarize(f)                              # build, NG setup, cells, UE lifecycle, counters
with open(log, errors="replace") as f:
    found = procedures.instances(f)                      # per procedure, its instances with stage times
# MAC UE Creation and the other MAC and scheduler procedures log their start at debug only.
create = found["UE Create"]
print(procedures.stats(procedures.latencies(create, "start", "done")))
```

Search entries are a timestamped line and its continuation lines, so filters match ASN.1 bodies too, and the
configuration echo is left out unless asked for. Time bounds are ISO dates or timestamps, with "T" or a space, or times
of day, compared to the same precision; `search.iso_bound()` gives a bound the "T" form. An RNTI filter matches `rnti=`,
`c-rnti=`, `tc-rnti=` and `crnti=` tokens, not `ra-rnti=` or `temp_crnti=`. Procedures are the `proc="..."` traces of
the DU (`PROCEDURES`): stages that a build does not log are skipped. `console.summarize()` reads the console output of
an application, `metrics_json.rollup()` its `metrics.json`, and `config.parse_config()` also gives the cell, AMF, duplex
and pcap options. The `ocudu_log_search.py`, `ocudu_log_summary.py` and `ocudu_ue_proc_latency.py` scripts of the
analyze-ran-log skill print them. `summary.summarize()` counts the handovers completed (`ho_count`) on the target side,
from info lines: intra-CU ones by `"Intra CU Handover Target Routine" finished successfully`, whose source side finishes
at debug, conditional intra-CU ones by `CHO inter-DU completion finalized`, and NGAP ones by their `HandoverNotify`. Xn
and conditional inter-CU handovers log no completion at info. `reconf_with_sync_count` counts the
`reconfigurationWithSync` of RRC bodies, which RRC logs at debug, and `ngap_handover_count` the NGAP `HandoverRequired`
and `HandoverRequest` PDUs. `app` is the application of the `==== <app> started ===` banner: `gNB`, `DU`, `CU`, `CU-CP`,
`CU-UP` or `DU low`, which log it and the build line under GNB (gNB and DU), CU, CU-CP, CU-UP or APP (DU low). `loggers`
are the loggers of the log's entries.

### UEs of a run

`parsers.correlate.ues.combine()` combines the UE contexts that the sources of a run see, the most reliable source
first, into one UE each with the identifiers of all of them:

```python
from parsers.correlate.ues import Context, combine

f1ap = [Context("f1ap.pcap", 0, 10.0, 20.5, {"rnti": "17921", "du_f1ap": "0", "cu_f1ap": "0"})]
log = [Context("gnb.log", 0, 9.9, 20.5, {"du_ue": "0", "rnti": "0x4601"})]
for ue in combine(f1ap, log):
    print(ue.key, ue.ids, ue.parts)  # f1ap.pcap:0 {"rnti": ["0x4601"], "du_f1ap": ["0"], "cu_f1ap": ["0"], "du_ue": ["0"]} ...
```

A context of a later source joins a UE of an earlier one by RNTI with overlapping lifetimes, a UE still open at the end
of its sources living to that end, or without RNTI by creation time, e.g. the target of a handover, which a log creates
before it knows its RNTI. RNTIs are compared as
`parsers.ran.rnti` normalizes them.

`parsers.correlate.run` reads the contexts from the files: `log_contexts()` from an OCUDU log (`events.UeTracker`),
`pcap_contexts()` from an NGAP, F1AP or E1AP pcap (`parsers.pcap.contexts`), `run_ues()` combines the F1AP pcap
and the logs of a run, and `run_traces()` also follows each UE through its contexts and its NGAP and E1AP contexts
(`ues.ue_traces()`): handovers by target C-RNTI, reestablishments by old C-RNTI, NGAP by NAS PDU or handover C-RNTI, E1AP
by UPF TEID:

```python
from parsers.correlate.run import run_traces
from parsers.pcap.run import find_pcaps
from parsers.pcap.tshark import Tshark

pcaps = find_pcaps("run_dir")
cores = [p for p in (pcaps["ngap"], pcaps["e1ap"]) if p]
ues, traces = run_traces(Tshark(), pcaps["f1ap"], ["run_dir/gnb.log"], cores)
for ue in ues:
    print(ue.t_start, ue.first("ue_trace"), ue.first("rnti"), ue.first("du_ue"), ue.first("cu_ue"), ue.first("ran_ngap"))
for trace in traces:
    print(trace.id, len(trace.ues), trace.ids.get("rnti"), trace.ids.get("amf_ngap"))
```

## Tests

```bash
python3 -m unittest discover -s tools/parsers/tests -t tools/parsers
```
