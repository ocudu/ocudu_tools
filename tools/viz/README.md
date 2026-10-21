# ocudu-viz

Browser-based visualizer for OCUDU artifacts. See [DESIGN.md](DESIGN.md) for requirements and decisions.

## Requirements

Docker (or Podman, see below). Nothing else: the image contains Python, the parsers and the built frontend.

## Usage

Run the `ocudu-viz` script in this directory, or put it on your `PATH` (e.g. a symlink in `~/.local/bin`):

```bash
tools/viz/ocudu-viz                          # nothing open, open from the page
tools/viz/ocudu-viz gnb.log                  # one file
tools/viz/ocudu-viz du1/du.log du2/du.log    # several files, one tab each
tools/viz/ocudu-viz run_dir/                 # the gNB artifacts of a directory, in one tab
```

Several servers can run at once, each on its own port. Name a session with `--name` to manage it: starting a session
with the name of a running one replaces it, keeping its port, while unnamed sessions are never touched. `--detach`
returns once the server is up:

```bash
tools/viz/ocudu-viz --name test --detach --no-browser run_dir/   # prints the URL and returns
tools/viz/ocudu-viz --list                                       # running sessions, named or not
tools/viz/ocudu-viz --stop test
```

Each tab shows a run: a single file, or the supported files of one directory, e.g. the `gnb.log` (or `du.log` and
`cu.log`) and the `mac`, `rlc`, `f1ap`, `e1ap` and `ngap` pcaps of a test run. Opening a file opens it alone; when
other files of its directory come from the same run, i.e. logs of the same build (named on their first line) and logs
or pcaps whose times overlap, a dialog offers to open the whole run in the tab. The files of a tab share its time
axis, its events and its zoom, and each plot picks its file.

Pcaps need `tshark`, which the container image has. Their frames are the records: clicking a message shows its frame
with the tshark summary, and the decoded frame beside it. F1AP, NGAP and E1AP pcaps give a `messages` dataset (a
table of their messages, with the RRC and NAS messages of F1AP) and events, with a trace row per UE context; MAC and
RLC pcaps give a `pdus` dataset. The `files` bar of a tab lists them: `✕` removes one, `+ file` adds another file of the same directory.
MAC and RLC pcaps of a run, which can be huge, are only parsed on request: with the `parse` button of their chip, when
a plot, table or grid picks them, which shows the progress, or when opened on their own; once cached, they open with
the run.

The `+` tab in the page browses the mounted directories (your home, the temp dir and any `--root`) and opens the
chosen file in a new tab. Its `Recent` view lists the last
files and directories opened in this browser, newest first, for one-click reopening. Close a tab with its `✕`.
Large logs are parsed in the background, with the progress shown on the tab and the file, and the page stays
usable meanwhile. Opened files stay open for as long as the server runs, also across page reloads.

The script builds the `ocudu-viz:latest` image when needed (cached after the first build), then runs it:

- Your home directory and the temp dir (`$TMPDIR` or `/tmp`, where gnb logs often end up) are mounted
  read-only at the same path, so files keep their host paths. Files outside them get their directory
  mounted too; use `--root DIR` to mount more directories, e.g. shared artifacts.
- The server is published on `127.0.0.1` only, on port 8765 or the next free one, and the browser opens on
  it once the files are parsed.
- The first open of a file parses it into a cache under `/tmp/ocudu-viz-<uid>/` on the host, which makes
  later opens instant.

In the page:

- Datasets whose rows belong to named entities get a second selector next to the dataset, e.g.
  `executors` lists the executors (`cu_cp_exec`, `mac_cell_exec#0`, ...). `all` selects every entity, which
  combines with split by to compare them.
- Pick source, dataset (METRICS layer), field and optionally a context field to split by, giving one
  series per value (e.g. one per `ue`), by default `pci` for `sched` and `rnti` for `sched_ue`. Without a selection of
  values, the first 256 are drawn, and the legend and statistics list the first 16 of them.
- `2nd` adds a second field of the same dataset to a time plot, e.g. `ul_brate` with `dl_brate`, with the same split,
  filter and instance. Its lines are dashed, in the color of the line of the first field of the same split value. Two
  fields of the same unit share the axis and scale; otherwise the second has its own axis on the right, and both axes
  are labelled with their field. Histograms show the first field only.
- Drag to zoom, Shift+drag to pan, double-click to reset. `W`/`S` zoom in and out around the middle of the view and
  `A`/`D` pan, within the time range of the files. The mouse wheel scrolls the page. The plots of a tab share the time
  axis.
  `reset zoom` shows the whole time range of the files of the tab, `fit plots` the time range of the data in its
  plots, tables and traces.
- The `events` chips above the plots mark log events on the time plots: `RACH` (PRACH detections), `RLF`,
  `warnings` and `errors`, with their count in the logs of the tab, greyed out when there are none. All start hidden.
  Hover a marker to see its event, click it to see its log line. The trace shows all the events. A warning or error
  repeated within a second, e.g. a late real-time worker, is one event: its hover gives the count and span of its
  lines, and its click opens the first one.
- `+ trace` adds a timeline of the UEs, of the F1AP pcap of the tab by default. With `log events` ticked (off by default), the UE events of
  the logs of the tab, e.g. random access, join the F1AP UE contexts, matched by C-RNTI and time, since pcaps
  identify UEs more reliably than logs. A trace of a log of a tab with an F1AP pcap also identifies its UEs by the F1AP
  UE contexts. A trace shows a bar per UE context, e.g. from its random access or creation
  to its deletion, with a glyph per event (random access, lifecycle, RRC, F1AP, NGAP, E1AP, mobility, failures), and
  a last row, `common`, with the events of no UE such as warnings and errors. Each UE has its own row, labelled with
  its UE index, RNTI and protocol identifiers; scroll the trace for more UEs. Hover a bar or glyph for details, click
  a glyph to see its log line or frame. The trace filter keeps the events matching an expression like the plot
  filter, over `type`, `category`, `layer`, `level`, `ue`, `rnti`, `cause` and `text`, and the UEs with such events,
  e.g. `rnti == 0x4602` for one UE or `type == prach or type == rlf`. Events without a UE index or RNTI take the
  ones of their UE. `rows by` shows a row per value of an identifier, labelled with both, e.g. `rnti=0x4601`,
  merging the UE contexts that share it: any identifier the rows have, `rnti` by default where they have one: `du_ue`
  and `cu_ue` (the DU and CU-CP UE indexes of logs, reused by later UEs), the identifiers of a pcap, e.g. `du_f1ap`,
  `cu_f1ap`, `ran_ngap` or `cu_cp_e1ap`, and, in a tab with an F1AP pcap and an NGAP pcap, the NGAP ids of the UE
  followed through its handovers and reestablishments: `amf_ngap`, which the AMF keeps across handovers, even between
  CUs, gives one row per UE. The F1AP pcap offers and hovers only `rnti` (the default), `du_ue`, `du_f1ap`, `cu_f1ap`
  and `amf_ngap`; an E1AP pcap its own ids, the default, and the NGAP ones of its UE; a log, all of them. Rows of UE
  contexts without the identifier are labelled `–`.
  Hovering a row shows the UE identifiers of the context it merges that the hovered event is of, or that is alive at
  the cursor; between the contexts of a row, e.g. while its UE is in another CU, only the value it is grouped by.
- `+ grid` shows the resource grid of the cell of a scheduler event trace (`schedtrace_cell<N>.bin`, written by OCUDU
  with `trace.schedtrace.enabled`), on the time axis of the plots, labelled in SFN.slot: CRBs upwards, one grid for TDD, DL above UL for FDD
  (told from the trace: FDD cells have PDSCH and PUSCH in the same slots). Zoomed out, it shows the occupancy of each
  group of 4 CRBs per bin of slots, colored by its dominant channel; within 4000 slots, per slot; within 400 slots, the
  allocations themselves with symbol resolution: PDSCH, SIB, RAR, paging, SSB, CSI-RS, PDCCH, PUSCH, PUCCH and PRACH
  occasions. Hover an
  allocation for its RNTI, CRBs, symbols, MCS, TBS, HARQ and timing, click it to see its slot event decoded, as tables
  of its decisions (one per kind of allocation), the allocation clicked highlighted, and of its events (HARQ-ACK, SR,
  CSI report, RACH), or as raw JSON, and click
  the occupancy to zoom to 40 slots around it. Traces are parsed on request, like MAC and RLC pcaps: a grid parses its
  trace when it opens.
- Right-click a PDSCH, a PUSCH or the PDCCH of a UE in the grid and `Inspect HARQ process` opens its transport block in
  a tab of the log pane: its PDCCHs, PDSCHs or PUSCHs, the UCI of its HARQ-ACK (PUCCH, or a PUSCH of the UE in that
  slot, marked `inferred`) and the HARQ-ACK reports, one row per PDU or report with its slot, transmission and
  parameters, from its new transmission to an ACK or the next new transmission on the process, and its outcome in the
  header (UL has none: the trace has no UL CRC). The grid outlines its allocations while the tab is shown. Click a row
  to mark its slot, double-click it to open its slot event.
- A scheduler event trace also gives plots and tables per UE: `ue_metrics`, per UE and 10 ms (throughput as METRICS
  `dl_brate`, from the ACKed TBS, scheduled DL and UL throughput, MCS, PRBs, new and retransmissions, ACK/NACK/DTX
  and error rate, CQI, RI, RSRP, SRs); `cell_metrics`, the scheduler latency, PRBs and UEs per 10 ms; and the grants
  and reports themselves (`dl_grants`, `ul_grants`, `harq_acks`, `csi_reports`). Click a point to see its slot event.
- `⛶` maximizes a plot, table, trace or grid: it fills the window, alone with the log pane, its chart taller (a trace
  showing more UE rows). `🗗` or Esc restores it.
- Click a point to see its log line and the lines around it; scrolling the log pane loads the lines before or after
  them as it nears its first or last line. Clicking a line of the log pane, or finding one with its search, marks its
  time on the plots, the trace and the grid with a gray dashed line, and the points of that line on the plots with a ring;
  the view pans to it when it is out of view. Drag the top edge of the log pane to resize it, double-click it to reset
  the height. Its `⛶` fills the window with it, e.g. to read a long decoded slot event; `🗗` or Esc restores it. Its
  search box finds text in the whole file, case-insensitive, from the highlighted line: Enter or `↓` goes to the next
  match, Shift+Enter or `↑` to the previous one, wrapping around the file, and the matches in view are highlighted.
  With `regex`, the text is a regular expression (Python syntax).
- Each record opens in a tab of the log pane: a log in one tab per file, a frame, slot event or HARQ transaction in a
  tab of its own.
  The tab in italics is a preview, which the next record opened replaces; double-click its label, or scroll, click or
  search in it, to keep it. Up to 8 tabs are kept, the least recently shown closed first; `×` (or a middle click)
  closes one, `✕` all. The plots mark the line of the shown tab, and the URL keeps the tabs kept.
- Right-click a `key=value` field of a line of the log pane, e.g. `tbs=5889`, and `Plot tbs=` plots its values in all
  the lines of the log over time (Shift+right-click for the browser's menu). The plot's header has a `filter` box, a
  line regex (e.g. `slot decisions`), and a `split by` box, a key of the same lines (e.g. `ue`, one series per UE);
  Enter applies them. Clicking a point shows only the matching lines in the log pane (`filtered`); `all lines` goes
  back to the whole log at the same line.
- Select text in a line of the log pane, e.g. `rnti=0x45` or `RLF`, right-click it, and `Mark lines with “…”` marks
  the lines of the log with that text, ignoring case, as events on the plots. Each mark gets a chip in the event bar
  to show, hide (click) or remove (✕) it; clicking a marked event shows the marked lines in the log pane.
- The top-right corner of a plot shows the time and value under the mouse. The legend below shows the time under
  the cursor.
- Filter the rows of a plot with an expression, applied on Enter. Values are in the base units of the
  parser (us, bps):

  ```text
  dl_mcs > 20 and pci == 1
  rnti == 0x4601 or (ta is not null and ta > 1.5)
  dl_mcs > ul_mcs
  ```

  Operators: `== != < <= > >=`, `is null`, `is not null`, combined with `and`, `or`, `not` and parentheses.
  Strings can be quoted or bare (`type == ue_create`). A bare word naming a field compares two fields.
- Switch a plot from `time series` to `histogram` to see the value distribution of the visible window,
  one curve per split value. Integer fields with few values (e.g. MCS) get one bin per value.
- `+ plot`, `+ table` and `+ trace` below the panels of a tab add a plot, a table or a trace. A table shows all the metrics of
  a layer for the visible window at full resolution, one row per metrics line. Type words in its column box
  to show only the metric columns whose name contains any of them (e.g. `latency brate`), and use the row
  filter like in plots. Click a row to see its log line. `export CSV` downloads all rows of the visible
  window with the shown columns, values in the canonical unit.
- `CSV` on a plot downloads the samples of its series in the visible window, at full resolution, one row per sample
  with its time and split value, in the canonical unit (e.g. bps). With a second field, one row per sample of either,
  with its field, value and unit.
- 💾 on a time plot opens it in a dialog to save as a PNG or SVG image, or copy it to the clipboard: on white with
  the light theme colors, at the size typed or dragged from its corner, with its title, legend and event markers. Its
  series are fetched again for a wider image. The SVG has vector lines and text, laid out like the plot.
- Below each plot, a table shows count, min, mean, p50, p95, p99 and max of each series in the visible
  window, with the plot filter applied. Percentiles are sampled for windows over 2M points.
- Time mode `relative` shows the time since the start of the files of the tab.

The current view (plots, splits, filters, zoom and time mode) is kept in the page URL, so a reload
restores it and a bookmark or shared link reopens it, as long as `ocudu-viz` runs with the same files.
Tabs are matched by name and plots by file name; tabs that are not open and plots of files not in their tab are
skipped with a warning.

The `theme` selector in the header switches between light and dark, or follows the
operating system with `auto`. The choice is remembered by the browser.

Large windows are downsampled to min/max per pixel, so spikes stay visible. Zoom in for full resolution.

## Options

| Option            | Description                                                                 |
|-------------------|-----------------------------------------------------------------------------|
| `--port N`        | Host port to serve on, or the next free one (default 8765).                 |
| `--root DIR`      | Also mount DIR read-only and open its files from the page. Repeatable.     |
| `--no-browser`    | Do not open the browser.                                                    |
| `--rebuild`       | Rebuild the image from scratch, without the build cache.                    |
| `--cache-size GB` | Cache size limit, least recently used caches are removed first (default 2). |
| `--no-cache`      | Keep parsed data in memory only.                                            |
| `--clear-cache`   | Remove all caches, and pcaps staged for tshark, before starting.            |
| `-j K`, `--jobs K` | Worker processes that parse large logs, 1 for none (default: one per CPU, up to 12). |
| `--clean`         | Remove the ocudu-viz images and parse caches, then exit.                    |

Set `OCUDU_VIZ_DOCKER=podman` to use Podman instead of Docker.

## Development

- `viz/`: Python package (FastAPI server, SQLite store, CLI run inside the container).
- `frontend/`: browser app, plain JavaScript modules with Vue and uPlot from npm, bundled by Vite into
  `viz/static/` (not in git). `THIRD-PARTY-LICENSES.txt` in the build lists the licenses of the bundled
  packages.
- `Dockerfile`: a Node stage builds the frontend, a Python stage installs the packages and serves it.

Python tests need no Node:

```bash
pip install -e tools/parsers -e "tools/viz[test]"
python3 -m unittest discover -s tools/viz/tests -t tools/viz
```

Frontend work outside the container needs Node 22:

```bash
cd tools/viz/frontend
npm ci
npm run build     # writes viz/static, served by a local "ocudu-viz FILE" from the Python package
npm run dev       # live-reloading dev server, proxying /api to an ocudu-viz server on port 8765
```
