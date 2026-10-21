# Visualizer design

Browser-based visualizer for OCUDU artifacts (`ocudu-viz`). Living document: requirements and decisions are
updated as they are agreed.

## Scope

- Input: OCUDU artifacts parsed with the `parsers` package. Log metrics first, then other log content
  (procedures, errors), pcaps and traces.
- Output: interactive views in the browser, opened locally against one or more files.
- Separate package from `parsers`. `viz` depends on `parsers`, never the opposite.

## Data model

- **Source:** one input file (e.g. `du.log`, `cu.log`, a pcap). A view can hold several sources.
- **Run:** the sources shown together in one tab, on one time axis: the gNB artifacts of one directory, or a single
  file (D16).
- **Dataset:** what a source type extracts from a source. Three kinds:
  - **Time series:** numeric fields over time, split by context (e.g. METRICS layers, trace latencies).
    A field is typed by its values; one without any, e.g. RSRP without CSI reports, by the type its source declares,
    else as a number when it has a unit, else as text, so that numeric fields stay in the plot field list even empty.
  - **Events:** timestamped points or intervals with a label (e.g. RRC procedures, errors, PRACH, handovers).
    Drawn as markers or spans over the plots.
  - **Records:** the raw entries behind the other datasets (e.g. log lines, packets), for drill-down.
- **Source type:** adapter from one artifact kind to datasets, built on `parsers`. Log metrics is the first
  source type. New artifact kinds add a source type, not changes to the views.

## Functional requirements

- **F1. Metric selection:** pick a dataset (e.g. METRICS layer `sched_ue`) and one or more of its fields.
- **F2. Split by context:** split a field into one series per context value (e.g. per `pci`, `ue`, `rnti`,
  `executor`).
- **F3. Filters:** boolean filter expressions over record fields (e.g. `pci == 1 and dl_mcs > 20`).
- **F4. Statistics:** min, max, mean and percentiles of the selected series over the visible range.
- **F5. Histogram view** of the selected field.
- **F6. Drill-down:** clicking a point or event shows the originating record and its neighbours.
- **F7. Units:** axes labelled with the dataset units (e.g. `MetricsParser.units`), auto-scaled for
  readability (e.g. us to ms).
- **F8. Time axis zoom and pan**, synchronized across all plots in a view.
- **F9. Multiple plots per view**, stacked with a shared time axis.
- **F10. Live tail:** follow a file that is still being written.
- **F11. Shareable state:** the current view (sources, datasets, fields, filters, time range) is encoded in
  the URL.
- **F12. Multiple sources:** `ocudu-viz cu.log du.log`. Each run, a file or the artifacts of one directory (D16), is
  shown in its own tab, with its own plots and zoom range; one tab is shown at a time.
- **F13. Time modes:** absolute (log wall clock, default) or relative (time since the start of the log).
- **F14. Events overlay:** event datasets drawn on top of the time series plots.
- **F15. Open files from the page:** start without files and open them from a file browser in the page.
- **F16. Table widgets and CSV export:** all the metrics of a dataset as a table, exportable as CSV.

## Non-functional requirements

- **N1. Data volume:** a single run can have ~1M metric records (e.g. 624k `sched_ue` lines from one
  multi-UE test). Interaction (zoom, pan, toggling series) must stay responsive at that size.
- **N2. Local first:** runs on a developer machine against local files, with one command and no manual
  build step.
- **N3. Offline:** no CDN or external services at runtime.
- **N4. Parsing stays in `parsers`:** the visualizer adds no artifact-format knowledge beyond mapping
  parser output to datasets.
- **N5. Container only for users:** running the visualizer needs only Docker (or Podman). Python, Node and
  all dependencies are inside the image. No third-party code is copied into the repository.
- **N6. Light runtime dependencies:** few, pure-pip Python packages. No pyarrow, pandas or system packages.

## Stack

| Layer          | Technology                                                       | Status   |
|----------------|------------------------------------------------------------------|----------|
| Backend        | FastAPI + uvicorn                                                | Decided  |
| Frontend       | Plain JS modules with JSDoc types, Vue 3, bundled by Vite        | Decided  |
| Charts         | uPlot                                                            | Decided  |
| Styling        | Plain CSS with custom properties                                 | Decided  |
| Record list    | Plain list of a window of records around the selection           | Decided  |
| Cache, queries | SQLite (Python standard library)                                 | Decided  |
| Packaging      | Docker image built locally by the `ocudu-viz` wrapper script     | Decided  |

Vue, uPlot and Vite come from npm, pinned in `frontend/package-lock.json`. The build writes the licenses of
the bundled packages to `THIRD-PARTY-LICENSES.txt`, served with the frontend.

## Decisions

### D1. Data transport

- JSON in columnar form (one array per column), e.g.
  `{"t": [...], "series": {"ue=1": [...]}, "record": [...]}`. Matches the uPlot data layout.
- Windows with up to ~50k points (to be tuned) are sent at full resolution.
- Larger windows are downsampled on the server to min/max per pixel bucket and series, so outliers stay
  visible. The browser sends its plot width.
- Zooming in fetches the new window at higher resolution.
- Every point carries the id of its record (e.g. log line number) for drill-down (F6).

### D2. Parse cache and query engine

- Each source is parsed once into a SQLite database: one table per dataset, one column per field, plus
  timestamp and record id. List values are stored as JSON text. Columns are added as fields appear.
- SQLite also serves the queries: filters (F3), splitting (F2), downsampling (D1) and statistics (F4).
- Filter expressions are translated from a small grammar (`field op value`, `and`, `or`, parentheses) into
  parameterized SQL, never passed through as SQL.
- Cache location: a per-user directory (mode 0700) under the system temp dir of the host (honours
  `$TMPDIR`), e.g. `/tmp/ocudu-viz-<uid>/`, mounted into the container at the same path. Cleared on reboot.
- Cache key: source path, size and mtime, plus the parser version, which holds a hash of the sources of the parsers
  package, since its version number is not bumped on changes. Stale caches are rebuilt.
- Size cap (default 2 GB), evicting the least recently used caches.
- Flags: `--cache-dir DIR` (persistent location), `--no-cache` (in-memory database), `--clear-cache`.
- Live tail (F10) appends rows to the source database.

### D3. Multiple sources

- Multiple sources from the first version (F12). Each source has its own cache database.
- Each run has its own tab, with its plots, zoom range and drill-down (D16). Only the selected tab is shown, so
  runs are not compared side by side.
- Absolute time by default, relative time as an option (F13).
- The time range of a source spans its first to last log timestamp, widened by its dataset timestamps if
  needed, since log lines are not strictly time ordered. It is the default view of its tab.
- Out of scope for the first version: automatic cross-source alignment (e.g. by RNTI or procedure), and
  source types not supported by `parsers` yet (e.g. UE logs).

### D4. First version scope

- In: CLI, log metrics source type, F1, F2, F6, F7, F8, F9, F12, F13, D1, D2.
- Deferred: F10 live tail, F14 events (since done, D14), Docker (since done, D12).
- Without explicit split values, only the first 256 series are returned, since sparse splits
  (e.g. ~1000 short-lived UEs) cannot be reduced by downsampling, and the statistics of the first 16, which the
  legends list too.

### D5. Layout and API

- Package `viz` (`ocudu-viz`): `cli.py`, `server.py`, `store.py`, `sources/` (one module per source type),
  `static/` (frontend build output, not in git).
- `frontend/`: the browser app sources (`src/`, `public/`), built by Vite into `viz/static/`.
- `Dockerfile` and the `ocudu-viz` wrapper script, see D12.
- `GET /api/sources`: sources with their datasets, fields (type, unit) and context fields.
- `GET /api/series?source&dataset&field&split_by&split_values&t0&t1&width`: one series per split value, each with its
  own timestamps, values and record ids. The browser aligns them with `uPlot.join`.
- `GET /api/context?source&dataset&field`: distinct values of a field, for split value selection.
- `GET /api/records?source&around&count`: raw records (log lines) around a record; with `after` or `before` instead of
  `around`, the count records after or before one.
- Sources sharing a file name are labelled with the first directory where their paths differ.

### D6. Filters and statistics

- Filters (F3) use the grammar in `viz/filters.py`: comparisons (`== != < <= > >=`, `is [not] null`)
  joined by `and`, `or`, `not` and parentheses. Operands are numbers, strings, hex values, bare words or
  other fields. Values are in the canonical units of `parsers`.
- RNTI fields (`rnti`, `c_rnti`, any name ending in `rnti`) compare in the form of `parsers.ran.rnti.normalize()`
  (`0x` and 4 lowercase digits): SQL normalizes their values, whether stored as `0x44`, `0x0044`, `0X4601` or 17921,
  and the operand is read as `parse_any()` reads a user's RNTI, so `rnti == 0x44`, `0x0044`, `44` and `'0x44'` select
  the same rows of a log, a schedtrace or a pcap. A bare number is hex unless too large for an RNTI, then decimal
  (`17921` is `0x4601`), as logs print RNTIs and the views show them, while a stored integer is the RNTI's value:
  `rnti == 44` selects a stored 68 (`0x0044`) and `rnti == 68` does not. Log events and lanes store their RNTI
  normalized, so that joins and grouping by RNTI agree with the other sources.
- A filter applies to the series and the statistics of its plot.
- Statistics (F4) per series over the visible window: count, min, max, mean from SQL; p50, p95, p99 in
  Python, from an evenly spaced sample when the window has more than 2M points.

### D7. Histogram

- Per plot toggle between time series and histogram (F5). The histogram covers the visible window, with the
  plot filter and split applied, and is drawn as one stepped curve per split value over shared bins.
- 50 equal bins over the window's value range. Integer fields spanning at most 50 values get one bin per
  integer, centred on it.

### D8. Dataset labels and instances

- A source type can give a dataset a display label and an instance field, the context field naming the
  entity each row belongs to. METRICS layer `exec` is shown as `executors`, with instance field `executor`.
- Datasets with an instance field get a second selector listing its values, the first selected by default,
  plus `all`. The selection restricts the rows on the server, so it applies to series, statistics and
  histograms.

### D9. View state in the URL

- The view (F11) is encoded as base64url JSON in the URL fragment (`#v=...`), updated in place with
  `history.replaceState` shortly after each change, and restored on load.
- Saved: time mode, the selected tab, and per tab its run, zoom range, shown event categories, the answer to whether
  to open the rest of its run (D16) and its widgets, each with its kind, source and settings: dataset, instance,
  field, second field, split, selected split values, filter, view mode, column filter, and for traces the joined
  sources and row identifier, and the kept tabs of its log pane (D19d). The state carries a version number; a state
  of another version is ignored.
- Tabs are saved by run name and matched by name to the open runs, and widgets by the file name of their source.
  Tabs of runs that are not open, and widgets of missing sources or datasets, are skipped with a warning. No file
  paths are put in the URL.

### D10. Themes

- Light and dark themes, defined as CSS custom properties, including the series colors (`--s0` to `--s9`).
  The dark theme uses neutral charcoal surfaces, soft grey text, faint grid lines and a muted blue
  accent.
- A header selector chooses `auto` (follows the OS setting), `light` or `dark`. The preference is kept in
  browser storage and applied before the first paint. Charts are rebuilt on a theme change, without
  refetching data.

### D11. Table widgets and CSV export

- Below the panels of a tab, `+ plot` adds a plot and `+ table` a table widget. A table shows a whole
  dataset of the tab's file: dataset and instance are selected, not a field. Its rows are the dataset rows of the visible window at full
  resolution, with the row filter applied: time, record id (log line), context fields, then all other fields.
- A column filter, applied as it is typed, shows the metric columns whose name contains any of its words.
  Context columns stay shown. Numeric columns are scaled to a display unit from the loaded rows.
- The page shows the first 1000 rows (`GET /api/table`). `GET /api/table.csv` streams all rows of the
  window with the shown columns, values in the canonical unit named in the column header (e.g.
  `dl_brate_bps`, `cpu_load_pct`), times in ISO 8601 UTC.
- Table widgets are saved in the URL view state with the plots of their tab.

### D12. Container

- The image (`ocudu-viz:latest`) is built locally from the `tools/` directory, by the `ocudu-viz` wrapper on
  every run, so that source changes are picked up; unchanged sources hit the build cache. A Node stage
  builds the frontend; a Python stage installs `parsers` and `viz` with the built frontend and runs the
  server. Users need no Python environment or Node.
- The wrapper mounts the home and temp (`$TMPDIR` or `/tmp`) directories read-only at the same path, plus
  the directory of any file outside them and any `--root DIR`, so files and links keep their host paths.
  The temp dir is included because gnb logs are often written there. The parse cache, under the temp dir,
  is mounted writable on top.
- The container runs as the host user, so that it can read the user's files and owns the cache it writes. It gets
  the host `HOME`, since that user has no home in the image, so that `~` in paths typed in the page is the host home.
- The server listens on all interfaces inside the container, and the port is published on the host
  loopback only (`127.0.0.1`), never on other interfaces.
- The wrapper opens the browser once the server listens, since a container cannot open the host browser.
- The image carries the label `org.ocudu.tool=ocudu-viz`. After each build the wrapper prunes the untagged
  images with that label, which rebuilds leave behind, and `--clean` removes all of them with the parse
  cache. Containers run with `--rm` and no Docker volumes are created.

### D13. Opening files from the page

- The server only lists and opens files under allowed roots: the directories given with `--root`, the
  home and temp directories by default. The wrapper passes the directories it mounts. Paths are resolved, with `~`
  for the home directory and following symlinks, before checking them against the roots.
- `GET /api/roots` and `GET /api/fs?path&hidden` list directories. `POST /api/runs {path}` opens a file or a
  directory as a run (D16): parsing runs in a background thread, and `/api/sources` reports each source's status
  (`deferred`, `parsing`, `ready`, `error`, `closed`) and progress, which the page polls while a source is parsing.
  A file given on the command line that fails to parse, e.g. a pcap of another protocol, is added in status `error`
  too, rather than stopping the server. `PcapSource.accepts()` takes any pcap, since its protocol is only known from
  tshark, too slow for listing directories, and the error on the page tells why it is not shown.
  Logs are files with a log entry in their first 200 lines or 64 KB, or in their last 64 KB, e.g. an excerpt that
  starts within the configuration echo, and empty `.log` files, of a run that wrote nothing yet. HTML renders of logs,
  e.g. the `gnb.log.html` and `stdout.log.html` of Retina, are not, so that a run does not hold its log twice: files
  named `.html` or `.htm`, starting with `<!DOCTYPE` or `<html`, or with an entry whose level is an HTML tag.
  Opening an open file selects its tab. `DELETE /api/runs/{id}` closes a run and releases the stores of its sources
  that no other run uses; `POST` and `DELETE /api/runs/{id}/sources` add and remove files of a run. Ids are not
  reused.
- Opened sources stay in the server for as long as it runs, so reloads keep them. The URL view state still
  contains no file paths.
- The file dialog has a `Recent` view with the last 15 files opened, kept in browser storage, so that it
  survives server restarts and reboots without a writable location outside the container. A recent file
  that fails to open is dropped from the list.

### D14. Events overlay

- Events are recognized by `parsers.log.events` in a second pass over the log, after the datasets are served, so that
  plots do not wait for them: on large logs, recognizing events takes longer than parsing the metrics. They are stored
  in their own cache file, built aside and renamed when complete, with their time, record, type, category, logger,
  level, UE, RNTI, cause and text. Source types without events skip the pass.
- `/api/sources` reports the state of the events of each source (`pending`, `parsing`, `ready`, `error`) and their
  count per category, and `GET /api/events?source&t0&t1&categories&ue&limit`
  returns the events of a window, capped at 5000.
- Events are dashed vertical lines over the time plots of the tab, coloured by category. Hovering one shows its text,
  clicking it opens its log line.
- The time plots mark only the log events that matter next to metrics, each with a chip and its count in the logs of
  the tab: `RACH` (PRACH detections), `RLF`, `warnings` and `errors`, disabled at 0 so that a clean log reads as one.
  `/api/events` selects events by category and by type. All start hidden, and the shown ones are kept in the URL view
  state. The trace shows all the events of all the categories.
- Repeated generic warnings and errors are stored as one event, since a log can repeat one millions of times (e.g. 3.1M
  OFH "woke up late" lines in a 485 MB log): the ones of the same logger, level, UE and message but for its numbers,
  each within 1 s of the previous one, up to 10 s, with their `count` and `span`. Event counts are of lines. They are
  collapsed in the worker of each chunk, so runs end at chunk edges. On the 485 MB log, 3.2M events become 11k rows,
  and the events phase takes 3.5 s instead of 8 s. A window holds a repeated event when it overlaps its span, so it
  shows in windows within the span too; the query bounds the start by the longest span of the source, which keeps the
  timestamp index in use.
- The `+ trace` widget shows the events per UE on the time axis of the tab: one bar per UE context, from its random
  access or creation to its deletion or failed creation, with a glyph per event, and a last row, `common`, with the events of no UE. Each UE has its own
  row, labelled with its DU UE index and RNTI, in start order. The trace shows 25 UE rows at a time and scrolls over the
  others with its own scrollbar, keeping the time axis and the row of events of no UE in view. Events are assigned to UE contexts by `parsers.log.events.UeTracker` while they are parsed: by
  RNTI, by DU UE index, and for CU-CP events by the RNTI of their CU-CP UE index, from its `UE created` line, else its
  RRC messages, so that a UE that arrived by handover has its own handover events. An all-zero RNTI is none. A UE created
  without RNTI, e.g. a handover target, gets it from its first `UE Configuration` line, within its lifetime only,
  since DU UE indexes are soon reused while RNTIs and protocol UE ids are not. A failed creation, e.g. on a DU UE index
  or C-RNTI clash, closes its lane, which is not open but `failed`, whatever window is read, and gives the index and
  RNTI back to the UE it clashed with.
  `GET /api/trace?source&t0&t1&max_lanes&limit` returns the UE contexts active in a window and its events.
- The trace has a filter like the plots (D6), over the event fields type, category, layer, level, ue, rnti, cause and
  text, evaluated by SQLite over the events of the window, with the UE index and RNTI of their lane for events
  without them. It keeps the matching events and the lanes they are in, and applies to joined traces too.
- Trace rows are the values of one identifier (`group_by`), the RNTI by default, among the ones of the lanes
  (`trace.lane_ids()`): the protocol ones of their labels, e.g. `du_f1ap`, the DU and CU-CP UE indexes of logs
  (`du_ue`, `cu_ue`), the RNTI, and the ids of the UEs of a run, e.g. `ue_trace` and `ran_ngap`; the page offers the
  ones the rows of the last trace have. Lanes sharing the value merge into one row, labelled
  with the identifier and value (e.g. `rnti=0x4601`), after the join and the filter; lanes without it keep their own
  row, labelled `–`. Each row lists the values of all the identifiers of the lanes it merges (`ids`)
  and the span and identifiers of each (`contexts`), which its events point to (`context`), so that hovering shows
  the ones of the hovered event or time.
- Most UE events are info lines, so logs whose layers log below info lack them; RLFs are warnings of the DU manager
  (`du`), so only logs below warning lack them. The log levels come from the
  configuration echo at the top of the log (`parsers.log.config`), and `/api/sources` reports a note naming the quiet
  layers and the events they hide, shown in the event bar and the trace.

### D15. Parallel parsing

- Most of the parse time of a metrics-heavy log goes to parsing the METRICS lines in Python, not to finding them, so
  the metrics of large logs are parsed in chunks by a pool of worker processes.
- The events of large logs too: the same chunks, which start at log entries, are parsed in the pool, and their events
  are assigned to UE contexts by `UeTracker` in the server process, in file order, since a UE context spans chunks.
  Most of the time of an event-heavy log goes to parsing each event rather than to finding the candidate lines, so a
  faster scan (e.g. RE2) gains little: on a 485 MB log of 3.2M events (mostly repeated warnings), 23 s in one process,
  8 s with 12 workers; on a 121 MB log of 250k events, 2.5 s and 0.9 s.
- The pool is shared by all sources for as long as the server runs, and started in the background when the server
  starts, so that the first large log does not wait for it. `-j K` sets its workers, 1 for no pool; by default there
  is one per CPU the process may use, up to 12: on a 16-core machine, a metrics-heavy log parses no faster with more. Workers start from a fork server, since forking the multithreaded server could deadlock. A pool whose
  worker dies is replaced, and its chunks are parsed in the server process.
- Chunks are byte ranges, about 4 per worker and at least 4 MB, so that logs under 8 MB, which parse as fast either
  way, stay in the server process, whose starts are moved to the next line that begins a
  log entry, so that multi-line entries stay whole. The chunking comes from `parsers.log.chunks`, which other tools use
  through `MetricsParser.parse_file()`; viz keeps the pool and its own workers, which return rows ready to store. Workers read their range from the file and return rows grouped by
  layer and grouped by field names, with values ready to store, the column types of each field and their units. Workers
  first count the lines of each chunk, so that they then number lines as in the whole file: line numbers are the
  record ids that link plots, tables and events to the log pane.
- The server process takes the results in file order, merges the units as a single pass would, and inserts each
  group of rows at once while later chunks are still parsed, leaving it mostly waiting for the workers. The cache has
  the same contents as with a single pass.

### D16. Runs

- A tab shows a run: one or more sources on one time axis. A run is either a single file, opened on its own, or the
  gNB artifacts of one directory: the gNB, DU or CU logs and the `mac`, `rlc`, `f1ap`, `e1ap` and `ngap` pcaps, and
  later `metrics.json`. Files of other nodes (5GC, UE simulator) and of other directories are not part of a run.
- Opening a directory, from the command line, opens a run with the supported files in it. Opening a file opens a run
  of that file, since files in e.g. `~/Downloads` or `/tmp` are often unrelated to each other. When other files of its
  directory were written by the same run, its tab asks whether to open them too, which makes it the run of the
  directory. The question is a dialog, shown as soon as the file is opened, and its answer is kept in the URL view
  state. Files are of the same run by content, not name, since names get edited: logs whose first line names the
  same build (`Built in <mode> mode using commit <hash> on branch <branch>`) and whose time spans overlap
  (`parsers.log.run`), and pcaps whose time spans, from the times in their packet headers, overlap. Overlaps allow
  60 s of slack. A run can add or remove files of its directory from its tab.
- Each source keeps its own parse, cache database, status and id. A run lists its sources, and the page polls them as
  today.
- Plots name their source: the dataset picker groups datasets by source, e.g. `du.log › sched_ue`, so one tab can
  plot metrics of several sources. The URL view state names the run of each tab and the source file of each widget
  (D9).
- Events and the trace merge the events of all the sources of a run. Pcaps give the protocol messages, i.e. F1AP,
  NGAP and E1AP messages, the RRC and NAS messages that F1AP carries, and failures, independently of log levels and
  log wording. Logs give what pcaps do not show, e.g. PRACH, contention resolution timeouts, RLF, warnings and errors.
  Logs and F1AP pcaps both show RRC messages, so a trace of the F1AP pcap with the events of the logs leaves out the RRC
  events of the logs. Linking a packet to the log line of the same message is open.
- UE contexts are joined across the sources of a run, since pcaps identify UEs more reliably than logs: a log can
  create a UE without its RNTI, e.g. the target of a handover, and log the RNTI of its later events only. The UEs of
  a run (`parsers.correlate.ues`) are its F1AP UE contexts with the UE lanes of its logs joined to them, each with the
  identifiers of all of them, whatever events are shown. The trace of an F1AP pcap of a run with logs is on these UEs
  (`/api/runs/{id}/trace`), with the events of the logs too with `log events`, off by default: a log lane with
  an RNTI joins the F1AP UE context with that C-RNTI whose lifetime, with 2 s of slack, overlaps it, and a log lane
  without one, which is rare, joins the context starting nearest its creation, within 1 s, i.e. its
  InitialULRRCMessageTransfer or UEContextSetup. The UEs are indexed by RNTI and by creation time, so that joining
  takes n log n. Log lanes of a random access of no UE, i.e. a PRACH only, are expected to have no context. Log lanes of no context keep their own row. The lane of a failed
  creation, a `failed` lane of its store, i.e. one with a `ue_create_failed` event, joins no UE and is a UE of its own
  (`failed` in the trace), since its DU UE index or RNTI is of another UE; grouping keeps it in its own row too, also in
  a window that ends before the failure. A trace of a log of a run with an F1AP pcap also shows
  its events on these UE contexts, the ones it has events in. RNTIs are compared as logs print them
  (`parsers.ran.rnti`). The F1AP gNB-DU-UE-F1AP-ID is not the DU UE index of the logs, so they are not compared. A
  trace row shows the DU UE index, RNTI and protocol identifiers of its UE.
- UEs are followed through their contexts and their NGAP and E1AP contexts (`parsers.correlate.ues.ue_traces()`), as
  the NGAP/E1AP ids of its UE trace on each lane, and the NGAP ones on the lanes of the E1AP pcap, whose trace the run
  trace serves too (`/api/runs/{id}/trace?sources=<NGAP or E1AP pcap>`). The UE trace number (`ue_trace`) is not
  shown: `amf_ngap` is the UE-level id, which the AMF keeps across handovers, also between CUs, so that it joins the
  visits of a UE to a gNB that the pcaps of the gNB cannot link. UEs without one (no NGAP pcap, or never registered)
  have no UE-level row. `ue_table.py` of the analyze-ran-log skill keeps the UE trace number. NGAP and E1AP contexts carry no RNTI and span the F1AP
  contexts of a UE (24 in a run of intra-CU handovers), so they join by values both sides carry, which the pcaps
  give as "links" (`parsers.pcap.messages.LINK_FIELDS`, kept per lane in `lane_info.links`): an F1AP context the one
  whose RRC reconfiguration named its C-RNTI as handover target (newUE-Identity), created within 1 s of it, or whose
  C-RNTI its reestablishment request carries, and the ones of the same CU-CP UE index (`cu_ue`) overlapping it; an NGAP
  context the F1AP ones of the same NAS PDUs, or of the C-RNTI of its handover acknowledge (a target gNB has no NAS),
  else the one created nearest within 1 s; an E1AP context the NGAP one of the same UPF TEID within 1 s, since test
  cores reuse TEIDs. The gNB's own TEIDs repeat across UEs, so they are not compared.
- Drill-down follows the source of the clicked item: log lines for logs, and the frame and its one-line tshark summary
  for pcaps, with the decoded frame on demand (D17).
- Sources of one run share the gNB clock, so they have no time offset.
- Steps: runs of several log sources; a pcap source type on `parsers.pcap`, with its events in the trace; the UE join
  and UE filter. The container image adds `tshark`.

### D17. Pcap sources

- `PcapSource` reads NGAP, F1AP, E1AP, MAC-NR and RLC-NR pcaps through `parsers.pcap`, which runs `tshark`. The
  protocol comes from the dissector of the first frame, not the file name. A pcap is read in one pass over its frames
  with the fields of its protocol only (`capture.read()`), since dissection takes the time and each field adds to it,
  and the events are built from the same pass. Without `tshark` on the `PATH`, pcaps are
  not supported files.
- Records are frames, identified by frame number, with their tshark summary (time, protocols, info) as text, stored in
  the cache. The record pane shows the decoded frame beside the frames (`/api/records/detail`), decoded on demand,
  with the 3GPP layers in full.
- Datasets: `messages` for NGAP, F1AP and E1AP, one row per message with its procedure, outcome, UE identifiers,
  cause, tshark summary and size, plus the RRC and NAS messages and C-RNTI for F1AP; `pdus` for MAC and RLC, one row
  per PDU with its size, direction and UE identifiers. The RLC UE id is `ue_index`, the DU UE index of the DU logs,
  since OCUDU writes `rlc-nr.ueid` as the index + 1.
- Events: one per NGAP, F1AP or E1AP message, in category `rrc` for F1AP messages carrying RRC, `failure` for
  unsuccessful outcomes, else `ngap`, `f1ap` or `e1ap`. Each UE context is a trace lane, from the first message with
  its identifiers until the response that releases it (UEContextRelease for NGAP and F1AP, bearerContextRelease for
  E1AP), since later UEs reuse the identifiers. Lanes are labelled with their identifiers, and F1AP lanes with their
  C-RNTI in hex, as logs print it. MAC and RLC pcaps have no events, so the trace does not offer them. Their UE id (`ue_index` in RLC, the RNTI
  in MAC) could give them UE lanes, joined to the UE contexts of the run by time.
- MAC and RLC pcaps (`PcapSource.on_request()`, by name) are opened "deferred" with the other files of a run, from the
  command line or the page, since tshark decodes some 40k MAC frames/s and they can be GBs, while they give only a
  `pdus` dataset: no events, no part in the trace or the UE join. `POST /api/sources/{id}/parse` parses one. A file
  opened on its own, or with a cache, is parsed at once.
- tshark reads pcaps through a link or copy under the cache directory only when it cannot read them in place, e.g.
  under the AppArmor profile of tshark on Ubuntu. `--clear-cache` removes them. The container image installs
  `tshark`.

### D18. Second metric of a plot

- A time plot can draw a second field of its dataset (`second`, `field2` in the view state), queried with the same
  split, filter and instance, since it is mostly the counterpart of the first (UL of DL, BLER of MCS). Fields of other
  datasets or sources would need their own split and filter.
- `chart-utils.plotData()` joins both into one chart: the second's series follow the first's, dashed, colored like the
  series of the first of the same split value. The same unit shares the "y" scale and display unit; different units,
  or both without, get a "y2" scale and a right axis, both axes then labelled with their field. A unit and no unit
  count as different. Histograms, which bin one value, show the first field only.
- Labels, statistics and the image legend name the field once there are two, e.g. `ul_brate rnti=0x4601`; the series
  cap applies per field. `/api/series.csv?field2` merges the samples of both in time order, with field, value and unit
  columns.

### D19. Search in the record pane

- `GET /api/records/search?source&q&regex&after&backward` returns the next record after a record, or the previous one,
  whose text matches, wrapping around the file (`Store.search()`). Matching is case-insensitive; `regex` takes a Python
  regular expression, else the text is matched literally.
- Logs are searched as bytes in blocks of about 8 MB that start at the record offsets of the store, so that the line
  of a match is the first line of its block plus the newlines before it, without reading the file from its start. A
  search that finds nothing reads the whole file: about 2 s for 485 MB. Pcaps search the frame summaries they store.
- A block with CRLF line ends is searched with them as LF, so that `$` matches at line ends. A regex valid as text but
  not as bytes, e.g. with `\N{...}`, is an invalid query (400).
- The pane highlights the matches in its lines with a JavaScript regular expression, which mostly agrees with Python's.

### D19a. Scrolling the record pane

- The pane first shows 61 records around the selected one, and loads 200 more before or after them when its lines are
  scrolled within 300 px of their first or last one (`/api/records?after` or `before`, so that sparse records, e.g. of
  an ad-hoc metric, page too), until the first or last record. It
  keeps at most 1000 records, dropping those at the other end, so that the page never holds a large part of a file,
  and keeps the lines in view where they were: the browser's scroll anchoring is off for the pane, which corrects the
  scroll position itself. Pages of an earlier selection or search are dropped when they arrive.
- The server reads a page of a log from the nearest record offset of the store at or before its first record, one
  every 1000 lines, so a page reads at most about 1200 lines of the file.

### D19a2. Focus on a record

- The record highlighted in the shown tab of the log pane, clicked, found or opened from a point, is the focus of the
  tab: the time plots, the trace and the grid draw a gray dashed line at its time (`--focus`), and plots of the same
  log, or of its ad-hoc metrics, a ring around its points. Its time is the timestamp at the start of its text, or of
  the entry it continues, so that it works for every source, whose records all start with one. When it is out of the
  view, the view pans to center it, keeping the zoom. Showing another tab of the pane focuses its record; closing the
  shown tab, or the pane, clears it.

### D19d. Tabs of the log pane

- Each record opened from a panel or an event gets a tab of the log pane, so that several can be compared: a log has
  one tab per source, an ad-hoc metric being a source of its own, which shows any of its lines; a frame, a slot event
  or a HARQ transaction (D20a), whose detail is the point, a tab of its own. Opening a record already in a tab shows
  that tab.
- A record is opened in the preview tab, in italics, which the next record opened replaces, as in editors, so that
  browsing points does not pile up tabs. A double-click on its label, or scrolling, clicking, searching or
  right-clicking in it, keeps it.
- At most 8 tabs, the least recently shown closed first, since each keeps up to 1000 records in the page. Hidden tabs
  keep their records, scroll position, search and highlighted record.
- Tabs belong to the tab of their run, and the URL view state saves those kept, by the file of their source; a tab of
  an ad-hoc metric is saved as the same line of its log.
- Labels are the file of the source, and for frames and slot events their record, the slot `SFN.slot` of a trace event.
  A HARQ transaction is saved by its allocation, and found again when restored.

### D19b. Ad-hoc metrics of logs

- Right-clicking a `key=value` field with a numeric value in a line of the log pane, e.g. `tbs=5889` of a slot decision
  line, or a selected key, opens a menu with `Plot tbs=`, which plots the numeric values of that key in all the lines
  of the log over time, at the time of their log entry. Values with a unit are scaled to the canonical unit of METRICS
  fields. Elsewhere, and with Shift, the browser's own menu opens, so that copying keeps working; a menu rather than a
  button picks the field under the pointer without selecting it first.
- An ad-hoc metric is a source of its own (`AdhocSource`, `parsers.log.adhoc`), derived from its log (`parent`) and
  opened with `POST /api/sources/{id}/adhoc {key, filter, split_key}`: the log is scanned in the worker pool, in the
  chunks of metrics parsing, finding the key in bytes and reading only the lines it is in, and the result is cached
  per spec like any parse. Its dataset `adhoc` has the values and, with a split key, the value of that key nearest
  before each value in its line, e.g. the `ue=` of a grant, so that plots, statistics, filters and CSV work as for
  other datasets. Its records are the lines with values only, stored as offsets (`record_lines`), not texts, so that
  the file is never held in memory.
- The plot of an ad-hoc metric is driven by text: its header names its log and key (e.g. `gnb.log › dl_bo=`) and has a
  `filter` box, a line regex (Python, case-insensitive), and a `split by` box, a key; Enter opens the ad-hoc metric of
  the new spec. The pickers of source, dataset and field, the second field and the metrics filter of other plots are
  left out, since an ad-hoc metric has one source, dataset and field, and its lines are filtered by text. Clicking a point opens the log pane on its records, i.e. the lines
  that match, marked `filtered`, paging through them; `all lines` shows the same line among all the lines of the
  log. Plot and table source pickers offer the ad-hoc metrics of the logs of the run.
- The URL view state saves an ad-hoc plot by its log and spec, and opens the metric again when restored.
- A line with several values and no split key gives one plotted point, the last one, since points are per record;
  splitting by a key of the line separates them.
- Ad-hoc metrics and marks are lines of their log and take its time shift in relative time.
- Filters match CRLF lines without their `\r`, so that `$` matches at their end (`adhoc.crlf_anchors()` for marks).

### D19c. Marked lines

- Right-clicking text selected in a line of the log pane also offers `Mark lines with “…”`: the lines of the log that
  contain the text, ignoring case, become events on the plots of the tab, as a mark. A mark is an ad-hoc source whose
  spec has a regex and no key (`parsers.log.adhoc.scan_lines()`); its records are the matching lines and its events,
  of category `adhoc` and type the regex, carry each line's text, logger, level, UE index and RNTI.
- Each mark has a chip in the event bar, with its count, to show or hide it, and ✕ to remove it. Its events are drawn
  with those of the chips of log events; clicking one opens the log pane on the matching lines only, `filtered`.
- The URL view state saves the marks of a tab by the file of their log and their regex.
- Marks are drawn on the plots, not yet in the trace, whose events are on the UE lanes of the events of a log.

### D20. Resource grid of scheduler event traces

- `SchedtraceSource` reads the `schedtrace_cell<N>.bin` of OCUDU with `parsers.schedtrace`, on request like MAC and RLC
  pcaps (traces of a few minutes are hundreds of MB). Its records are the events, the text a summary line and the
  detail the decoded event. Its views are the `grid` panel and plots and tables of its datasets.
- Its datasets are the per-UE metrics of `parsers.schedtrace.ue_metrics`: `ue_metrics`, aggregates per UE and 10 ms bin,
  named as the fields of the METRICS `Scheduler UE` lines where they mean the same (e.g. `dl_brate`, the ACKed DL TBS,
  `dl_error_rate`, `cqi`), so that a plot of either reads the same, though means unrounded, as statistics over bins
  need, where METRICS rounds `dl_mcs` and `cqi`; `cell_metrics`, the scheduler latency, PRBs and UEs of the cell per
  bin; and one row per grant or report, `dl_grants`, `ul_grants`, `harq_acks` and `csi_reports`, for exact values. 10 ms
  bins, one radio frame, keep the scheduler dynamics that METRICS periods of a second average out. The trace has no UL
  CRC or buffer status, so UL rates are of scheduled new transmissions (`ul_sched_brate`). Workers sum the bins of their
  chunk and the server process merges the sums, as for the grid occupancy, and every row links to its slot event. New
  plots of a trace show `ue_metrics.dl_brate` split by RNTI.
- Msg3 PUSCHs carry the invalid DU UE index of OCUDU (8192), so their `ue_index` is null, and they count under their
  TC-RNTI: a split by `ue_index` has no fake UE, and a bin of the RNTI takes the index of its later grants and reports.
- `parsers.schedtrace.grid` places the allocations of a slot as rectangles of symbols and CRBs. The trace has no
  VRB-to-PRB interleaving, RBG configuration or SRS frequency resources, so VRBs map to PRBs one to one, RBGs have the
  configuration 1 size of the BWP, and SRS is left out. PDSCHs of DCI format 1_0 in a common search space are placed
  from the CORESET of their PDCCH, matched to it in the slot, PDCCHs from the REG bundles of their CCEs, but those of
  an aggregation level or CCEs out of range of their CORESET, as of a corrupt event, which are left out, and PRACH
  occasions from msg1-FrequencyStart and their format; long PRACH formats last past their slot, counted in the
  occupancy only in their first. The UL allocations of a slot decision are transmitted in its slot.
- The grid shares the time axis of the plots, at which allocations are sub-pixel, so it changes its level of detail
  with the window (`/api/grid`): within 400 slots the allocations, decoded on demand from the file offsets of the
  slots (`grid_slots`); within 4000 slots their occupancy per slot, decoded too; beyond, the occupancy stored at
  parse time (`grid_cells`) per bins of 20, 100, 400 or 2000 slots, the finest with at most one bin per pixel. Cells
  are groups of 4 CRBs, shown in the color of their dominant channel, shaded by their total occupancy, since TDD bins
  mix DL and UL.
- Channel colors are `--ch-<channel>` of the theme. The light theme takes the light half of Tableau 20, but for a
  bluer purple for SSBs, apart from the pink of PDCCHs; the dark theme takes the muted tones of gruvbox, with beige
  SIBs and faded orange CSI-RS. Allocations are filled with their channel color, as in the legend, and outlined with
  it darkened, so that adjacent allocations of a channel stay apart.
- Traces start with the header of their file format version (`parsers.schedtrace`): traces of another major version,
  or written before OCUDU versioned the format, fail to parse with that error, shown on the page.
- An event whose fields do not decode ends the trace, with a warning, as in `parsers.schedtrace`: the events from it
  are left out, the run identity of the trace is unknown, and its record detail is an error (400).
- Parsing splits the trace at event boundaries, from the size prefixes, into about 4 chunks per worker of at least
  2000 events, parsed in the worker pool with the decoder generated from the schemas, decoding only the fields that
  placing allocations needs (`grid_only`). Workers return the grid occupancy of the bins within their chunk ready to
  store, one row per bin, and the parts of the bins that span chunks, which the server process merges. A 325 MB trace
  parses in 3.2 s and a 73 MB one in 1.4 s with 12 workers.
- Slots are placed on a grid of slot duration by their `slot_tx`, unwrapped across SFN wraps, at the median
  difference between their timestamps and that grid, rather than at their timestamps: these are stamped when each event
  is made and jitter by up to a few hundred µs, which made the allocations of consecutive slots overlap or leave gaps.
  The median keeps the slots of a window at their times, so that the grid stays aligned with the plots.
- The time axis of the grid is labelled in SFN.slot from the `slot_tx` of the slots, which `/api/grid` sends as
  "anchors" (all of a window, or about one per pixel): ticks are placed at round slot counts (1, 2, 5, 10 slots, or
  1 to 500 frames, short of the SFN wrap) from the nearest anchor, and labelled by the count they were placed at,
  since mapping a time back to a slot can land on the next slot, slot times jittering around their nominal spacing.
- Duplex is not in the trace: a cell is FDD when at least 5% of its slots with PDSCH, PUSCH or PUCCH have DL and UL
  together, which TDD cells only have in special slots, without PDSCH and PUSCH together.

### D20a. HARQ transactions

- Right-clicking a PDSCH, a PUSCH or the PDCCH of a UE (not of SI-RNTI, RA-RNTI or P-RNTI) in the grid offers
  `Inspect HARQ process`, which opens the HARQ transaction of the allocation in a tab of the log pane (D19d): the life of
  its transport block on its process, grouped by `parsers.schedtrace.harq` by RNTI and HARQ id from a new transmission
  (`nof_retxs` 0, since PDSCHs and PUSCHs have no NDI), with its PDCCHs, UCI and HARQ-ACK reports, until an Ack or the
  next new transmission on the process. A menu, since a click opens the slot event.
- `GET /api/harq?source&record&item` takes what the grid has, the slot event and `list:index` of the allocation, and
  decodes on demand the slot events within 1024 slots (one SFN cycle) before and after it, stopping when the
  transaction ends: a transaction lasts tens of slots, so nothing is stored at parse time. Rows are timed as the grid
  places their slots.
- The tab shows the outcome in its header, and a table of one row per PDU or report: time after the first row, SFN.slot,
  kind, transmission, RNTI, and the fields of the kind as `key=value` text, rather than a wide table of every field,
  mostly empty. A click on a row focuses its slot (D19a2), a double-click opens its slot event. Its label is
  `HARQ <dir> <rnti> h<id> · <SFN.slot>` of its first row.
- While the tab is shown, the grid outlines the allocations of the transaction in `--focus` gray, outside their own
  outline. Inspecting an allocation of a transaction already in a tab shows that tab.
- The trace does not say which PUSCH carries UCI, nor has it UL CRC or HARQ timeouts: a PUSCH of the UE in the UCI slot
  of a PDSCH without a PUCCH with HARQ-ACK bits is shown as `UCI on PUSCH`, inferred; UL transactions have no outcome;
  a DL transaction without Ack ends at the next new transmission on its process.
- PUCCHs are not entry points, since one carries the HARQ-ACK of several PDSCHs. Log lines of the transaction are not
  shown.

### D21. Maximized panels and log pane

- A panel's `⛶` hides the other panels of its tab and fills the space the app bar and the log pane leave, its chart or
  rows taller; the log pane stays, since clicking the panel opens records in it.
- The log pane's `⛶` fills the whole window, over the app bar, with the lines and the decoded record of its shown tab
  taking the height below its bars; it keeps the highlighted line in view. Its drag-resized height is kept for when it is restored.
- `🗗` or Esc restores either. Esc restores the log pane first, when both are maximized.

## Open questions

None yet.
