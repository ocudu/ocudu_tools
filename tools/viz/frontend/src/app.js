// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import { createApp } from "vue";
import "uplot/dist/uPlot.min.css";
import "./style.css";
import { getJSON, postJSON } from "./api.js";
import FileBrowser from "./components/file-browser.js";
import PlotPanel from "./components/plot-panel.js";
import RecordPane from "./components/record-pane.js";
import TablePanel from "./components/table-panel.js";
import TracePanel from "./components/trace-panel.js";
import GridPanel from "./components/grid-panel.js";
import { applyTheme, loadThemePreference, onSystemThemeChange, saveThemePreference } from "./theme.js";
import { addRecent, removeRecent } from "./recent.js";
import { decodeView, encodeView } from "./view-state.js";

// Events that the time plots can mark, in display order: each a whole category of the events of logs, or one type of
// event of a category. The trace shows all the events.
const PLOT_EVENTS = [
  { key: "rach", label: "RACH", category: "ra", type: "prach" },
  { key: "rlf", label: "RLF", category: "failure", type: "rlf" },
  { key: "warning", label: "warnings", category: "warning" },
  { key: "error", label: "errors", category: "error" },
];
// Events fetched for the visible window of a tab, per source.
const MAX_EVENTS = 5000;
// Delay before fetching events after the view changes, to coalesce zoom and pan events.
const EVENTS_FETCH_DELAY_MS = 150;
// Tabs of the log pane kept at most, the least recently shown closed first.
const MAX_PANE_TABS = 8;
// Interval between refreshes of the sources while some are being parsed.
const SOURCES_POLL_MS = 500;

/** Returns whether a source or its events are still being parsed. */
function isParsing(s) {
  return s.status === "parsing" || (s.status === "ready" && ["pending", "parsing"].includes(s.events_status));
}
// Factor of the time range of a zoom in or out with the keyboard, and fraction of it that a pan moves.
const KEY_ZOOM_FACTOR = 1.5;
const KEY_PAN_FRACTION = 0.2;
// Delay before writing the view to the URL, to coalesce zoom and pan events.
const URL_UPDATE_DELAY_MS = 300;

let nextPlotId = 1;
// Ids of the tabs of the log pane, and the count of tab activations, which orders them by use.
let nextPaneTabId = 1;
let paneTabUses = 0;

/**
 * Creates a widget: a plot, a table of a whole dataset with kind "table", the trace with kind "trace", or the resource
 * grid of a scheduler event trace with kind "grid".
 * @param {{source: number, dataset?: string | null, kind?: string} & Record<string, any>} init
 */
function newPlot(init) {
  return {
    kind: "plot",
    columnFilter: "",
    field: null,
    // Second field drawn with the first when second is set, see the plot panel.
    second: false,
    field2: null,
    splitBy: null,
    splitValues: [],
    filter: "",
    mode: "time",
    instance: null,
    groupBy: null,
    ...init,
    dataset: init.dataset ?? null,
    id: nextPlotId++,
  };
}

/**
 * State of the tab of one run: its plots, zoom range and shown event categories, and for a run of one file, the
 * other files of its run once checked (related), and whether the question to open them was answered.
 */
function newTab() {
  // focused is the id of the panel maximized, alone with the log pane, or null.
  // marks are the lines marked as events on the plots: their ad-hoc source, regex, log and whether they are shown.
  // pane holds the tabs of the log pane and the id of the shown one.
  return {
    plots: [], userRange: null, defaultPlotAdded: false, eventCategories: [], marks: [], related: null, relatedAnswered: false,
    focused: null, pane: { tabs: [], active: null },
  };
}

const App = {
  components: { FileBrowser, GridPanel, PlotPanel, RecordPane, TablePanel, TracePanel },
  data() {
    return {
      // Open runs, each with the ids of its sources.
      runs: [],
      // Sources by id, including closed ones.
      sources: [],
      // Tab state by run id.
      tabs: {},
      // Height of a maximized panel, in CSS pixels: the window less the app bar and the log pane.
      maximizedHeight: 0,
      activeId: null,
      timeMode: "absolute",
      // Record highlighted in the log pane, as {source, record, t} in display time, which the panels mark.
      focus: null,
      error: "",
      warnings: [],
      themePref: loadThemePreference(),
      themeVersion: 0,
      roots: [],
      canOpen: false,
      // Events of the visible window of the active tab, in display time, each with its source.
      events: [],
      eventsTruncated: false,
      browserOpen: false,
      // Supported files of the directory of the active run, while its file list is shown.
      runFiles: null,
    };
  },
  computed: {
    activeRun() {
      return this.runs.find((r) => r.id === this.activeId) ?? null;
    },
    activeTab() {
      return this.activeId == null ? null : this.tabs[this.activeId] ?? null;
    },
    /** Sources of the active run. */
    runSources() {
      return (this.activeRun?.sources ?? []).map((id) => this.sources[id]).filter(Boolean);
    },
    /** Tabs of the log pane of the active tab. */
    paneTabs() {
      return this.activeTab?.pane.tabs ?? [];
    },
    readySources() {
      return this.runSources.filter((s) => s.status === "ready");
    },
    /** Sources that plots and tables offer: those of the active run, then the ad-hoc metrics of its logs. */
    panelSources() {
      const ids = new Set(this.activeRun?.sources ?? []);
      return [...this.runSources, ...this.sources.filter((s) => s.parent != null && ids.has(s.parent) && s.status !== "closed")];
    },
    /** Shift from source time to display time, per source id: in relative time, from the start of its run. */
    shifts() {
      const shifts = this.sources.map(() => 0);
      if (this.timeMode !== "relative") return shifts;
      // The active run comes last, so that the shift of a source in several runs is the one of the shown run.
      const runs = [...this.runs.filter((r) => r.id !== this.activeId), ...(this.activeRun ? [this.activeRun] : [])];
      for (const run of runs) {
        const span = this.runSpan(run);
        if (!span) continue;
        for (const id of run.sources) shifts[id] = -span.min;
      }
      // Ad-hoc metrics and marks are lines of their log, whose shift they take.
      for (const s of this.sources) if (s.parent != null) shifts[s.id] = shifts[s.parent];
      return shifts;
    },
    fullRange() {
      const span = this.activeRun && this.runSpan(this.activeRun);
      if (!span) return null;
      const shift = this.shifts[this.activeRun.sources[0]];
      return { min: span.min + shift, max: (span.max > span.min ? span.max : span.min + 1) + shift };
    },
    /** Inputs of the events request of the active tab. */
    eventsQuery() {
      const states = this.runSources.map((s) => [s.id, s.status, s.events_status]);
      const marks = (this.activeTab?.marks ?? []).map((m) => [m.source, m.shown, this.sources[m.source]?.events_status]);
      return [this.activeId, states, this.view, this.activeTab?.eventCategories, marks, this.timeMode];
    },
    /** Logs of the active run whose events are parsed, the sources of the events marked on the plots. */
    eventLogs() {
      return this.readySources.filter((s) => s.type !== "pcap" && s.events_status === "ready");
    },
    /**
     * PLOT_EVENTS with their count in the logs of the active run, all shown once a log is parsed, so that a clean log
     * reads as one.
     */
    eventCategoryChips() {
      if (!this.eventLogs.length) return [];
      return PLOT_EVENTS.map((kind) => {
        const counts = (s) => (kind.type ? s.event_type_counts?.[kind.type] : s.event_counts?.[kind.category]) ?? 0;
        return { ...kind, count: this.eventLogs.reduce((sum, s) => sum + counts(s), 0) };
      });
    },
    /** Notes about the sources of the active run, e.g. why some events are missing. */
    activeNotes() {
      const several = this.runSources.length > 1;
      return this.runSources.flatMap((s) => (s.notes ?? []).map((n) => (several ? `${s.file}: ${n}` : n)));
    },
    /** State of the events of the active run while some are not ready: "parsing" or "error". */
    eventsState() {
      const states = this.readySources.map((s) => s.events_status);
      if (states.some((st) => st === "pending" || st === "parsing")) return "parsing";
      return states.includes("error") ? "error" : null;
    },
    /** Parsing progress of the active run, while some of its sources are parsing. */
    runProgress() {
      return this.progress(this.activeRun);
    },
    view() {
      return this.activeTab?.userRange ?? this.fullRange;
    },
    /** Whether to ask to open the other files of the run of the active single-file tab. */
    relatedQuestion() {
      return Boolean(this.activeRun?.kind === "file" && this.activeTab?.related?.length && !this.activeTab.relatedAnswered);
    },
    /**
     * Span of the data shown by the panels of the active tab: the dataset of each plot and table and the file of each
     * trace, whose events all show. Without panels, the span of all the datasets of the tab.
     */
    panelsRange() {
      if (!this.readySources.length) return null;
      const ready = new Map(this.readySources.map((s) => [s.id, s]));
      const plots = this.activeTab?.plots ?? [];
      let spans;
      if (plots.length) {
        spans = plots.flatMap((p) => {
          const s = ready.get(p.source);
          if (!s) return [];
          if (p.kind === "trace" || p.kind === "grid") return s.t_min != null ? [[s.t_min, s.t_max ?? s.t_min, s.id]] : [];
          const d = s.datasets.find((d) => d.name === p.dataset);
          return d?.t_min != null ? [[d.t_min, d.t_max, s.id]] : [];
        });
      } else {
        spans = this.readySources.flatMap((s) => s.datasets.filter((d) => d.t_min != null).map((d) => [d.t_min, d.t_max, s.id]));
      }
      spans = spans.map(([min, max, id]) => [min + this.shifts[id], max + this.shifts[id]]);
      if (!spans.length) return null;
      const min = Math.min(...spans.map((x) => x[0]));
      const max = Math.max(...spans.map((x) => x[1]));
      return { min, max: max > min ? max : min + 1 };
    },
  },
  watch: {
    // The log pane opens and closes with its tabs, changing the height of a maximized panel.
    async "paneTabs.length"() {
      await this.$nextTick();
      this.observeHeights();
    },
    themePref(pref) {
      saveThemePreference(pref);
      applyTheme(pref);
      this.themeVersion++;
    },
    timeMode() {
      // Zoom ranges are in display time, which the time mode changes.
      if (this.restoring) return;
      for (const tab of Object.values(this.tabs)) tab.userRange = null;
    },
    activeId() {
      // The panes of other tabs keep their tabs, whose records refocus when shown.
      this.focus = null;
      this.runFiles = null;
    },
    async relatedQuestion(shown) {
      // Enter opens the run, Escape keeps the single file.
      if (!shown) return;
      await this.$nextTick();
      this.$refs.openRunButton?.focus();
    },
    eventsQuery: {
      handler() {
        clearTimeout(this.eventsTimer);
        this.eventsTimer = setTimeout(() => this.fetchEvents(), EVENTS_FETCH_DELAY_MS);
      },
      deep: true,
    },
  },
  created() {
    // Not reactive: URL synchronization state.
    this.restoring = false;
    this.urlTimer = null;
    this.writtenHash = "";
    // Polling of the sources while some are being parsed.
    this.pollTimer = null;
    // Pending and in-flight events requests.
    this.eventsTimer = null;
    this.eventsAbort = null;
    // Ids of the sources whose parsing error was already reported.
    this.reportedErrors = new Set();
    applyTheme(this.themePref);
    onSystemThemeChange(() => {
      if (this.themePref !== "auto") return;
      applyTheme("auto");
      this.themeVersion++;
    });
  },
  async mounted() {
    try {
      const [runs, sources, roots] = await Promise.all([getJSON("/api/runs"), getJSON("/api/sources"), getJSON("/api/roots")]);
      this.runs = runs;
      this.sources = sources;
      this.roots = roots.roots;
      this.canOpen = roots.can_open;
    } catch (e) {
      this.error = e.message;
      return;
    }
    await this.restoreView();
    this.syncTabs();
    if (this.runSourcesOf(this.runs).some(isParsing)) this.pollSources();

    this.$watch(
      () => [this.tabs, this.activeId, this.timeMode],
      () => this.scheduleUrlUpdate(),
      { deep: true },
    );
    // A pasted link only changes the fragment, which does not reload the page.
    window.addEventListener("hashchange", () => {
      if (location.hash !== this.writtenHash) location.reload();
    });
    window.addEventListener("keydown", (e) => this.onKey(e));
    // A maximized panel fills the space the app bar and the log pane leave, both of which can change.
    this.heightObserver = new ResizeObserver(() => this.updateMaximizedHeight());
    window.addEventListener("resize", () => this.updateMaximizedHeight());
    this.updateMaximizedHeight();
  },
  methods: {
    async restoreView() {
      const restored = decodeView(location.hash, this.runs, this.sources);
      if (!restored) return;
      this.warnings = restored.warnings;
      const view = restored.view;
      if (!view) return;
      this.restoring = true;
      this.timeMode = view.timeMode === "relative" ? "relative" : "absolute";
      // Zoom ranges are in display time, which depends on the time mode set above.
      await this.$nextTick();
      for (const t of view.tabs) {
        const tab = newTab();
        tab.userRange = t.range && t.range.max > t.range.min ? t.range : null;
        tab.plots = t.plots.map((p) => newPlot(p));
        tab.eventCategories = (t.eventCategories ?? []).filter((key) => PLOT_EVENTS.some((kind) => kind.key === key));
        tab.relatedAnswered = t.relatedAnswered;
        tab.defaultPlotAdded = true;
        this.tabs[t.run] = tab;
        // Ad-hoc metrics are opened again from their log, which their plots name. The plots of the reactive tab are
        // passed, so that the panels see them change.
        for (const p of this.tabs[t.run].plots.filter((p) => p.adhoc)) this.plotAdhoc({ source: p.source, ...p.adhoc }, p);
        for (const m of t.marks) this.markLines(m, m.shown, this.tabs[t.run]);
        for (const r of t.pane) this.openRecord(r, r.kind, this.tabs[t.run]).preview = false;
        const shown = t.pane.findIndex((r) => r.active);
        if (shown >= 0) this.activatePaneTab(this.tabs[t.run].pane.tabs[shown].id, this.tabs[t.run]);
      }
      if (view.active != null) this.activeId = view.active;
      this.restoring = false;
    },

    scheduleUrlUpdate() {
      clearTimeout(this.urlTimer);
      this.urlTimer = setTimeout(() => {
        this.writtenHash = encodeView(this);
        history.replaceState(null, "", this.writtenHash);
      }, URL_UPDATE_DELAY_MS);
    },

    /** Sources of the given runs. */
    runSourcesOf(runs) {
      return runs.flatMap((r) => r.sources.map((id) => this.sources[id]).filter(Boolean));
    },

    /** Time span of the parsed sources of a run, in source time, or null before any is parsed. */
    runSpan(run) {
      const ready = this.runSourcesOf([run]).filter((s) => s.status === "ready" && s.t_min != null);
      if (!ready.length) return null;
      return { min: Math.min(...ready.map((s) => s.t_min)), max: Math.max(...ready.map((s) => s.t_max ?? s.t_min)) };
    },

    /** Mean parsing progress of the sources of a run, or null when none is parsing. */
    progress(run) {
      const sources = run ? this.runSourcesOf([run]) : [];
      if (!sources.some((s) => s.status === "parsing")) return null;
      return sources.reduce((sum, s) => sum + (s.status === "parsing" ? s.progress : 1), 0) / sources.length;
    },

    /**
     * Parsed source of a run for a new panel: a log for plots and tables, since logs have the metrics, and for a trace
     * the F1AP pcap, else the NGAP pcap, else the first source with events. Pcaps are told by their event categories.
     */
    defaultSource(run, kind = "plot") {
      if (kind === "grid") {
        // Scheduler event traces are parsed on request, so the grid also takes one that is not yet.
        const traces = this.runSourcesOf([run]).filter((s) => s.type === "schedtrace");
        return traces.find((s) => s.status === "ready") ?? traces[0] ?? null;
      }
      const ready = this.runSourcesOf([run]).filter((s) => s.status === "ready");
      if (kind === "trace") {
        const pcapWith = (category) => ready.find((s) => s.type === "pcap" && s.event_counts?.[category]);
        const withEvents = ready.find((s) => Object.keys(s.event_counts ?? {}).length);
        return pcapWith("f1ap") ?? pcapWith("ngap") ?? withEvents ?? ready[0] ?? null;
      }
      return ready.find((s) => s.type !== "pcap" && s.datasets.length) ?? ready.find((s) => s.datasets.length) ?? ready[0] ?? null;
    },

    /** Creates the tabs of new runs, drops the ones of closed runs, and keeps a tab selected. */
    syncTabs() {
      const open = new Set(this.runs.map((r) => r.id));
      for (const id of Object.keys(this.tabs)) {
        if (!open.has(Number(id))) delete this.tabs[id];
      }
      for (const run of this.runs) {
        if (!this.tabs[run.id]) this.tabs[run.id] = newTab();
        const tab = this.tabs[run.id];
        if (run.kind === "file" && tab.related == null) this.checkRelated(run.id);
        // A run starts with one plot, once, so that removing all plots is not undone. It waits for a log, which has the
        // metrics, unless no log is coming.
        const sources = this.runSourcesOf([run]);
        const logReady = sources.some((s) => s.status === "ready" && s.type !== "pcap" && s.datasets.length);
        const source = logReady || !sources.some((s) => s.status === "parsing") ? this.defaultSource(run) : null;
        if (source && !tab.defaultPlotAdded) {
          tab.defaultPlotAdded = true;
          addRecent(run.path, run.kind === "dir");
          if (!tab.plots.length) tab.plots.push(newPlot({ source: source.id }));
        }
      }
      if (this.activeId == null || !open.has(this.activeId)) this.activeId = this.runs[0]?.id ?? null;
    },

    /** Looks for the other files of the run of a single-file tab, once. */
    async checkRelated(runId) {
      const tab = this.tabs[runId];
      tab.related = [];
      try {
        tab.related = await getJSON(`/api/runs/${runId}/related`);
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
      }
    },

    /** Opens the other files of the run of the active tab in it, or switches to the tab of its directory. */
    async promoteRun() {
      const tab = this.activeTab;
      tab.relatedAnswered = true;
      let run;
      try {
        run = await postJSON(`/api/runs/${this.activeId}/promote`, {});
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
        return;
      }
      if (run.id !== this.activeId) delete this.tabs[this.activeId];
      await this.pollSources();
      this.activeId = run.id;
    },

    /** @param {string} key Key of one of PLOT_EVENTS. */
    toggleEventCategory(key) {
      const tab = this.activeTab;
      const shown = new Set(tab.eventCategories ?? []);
      if (shown.has(key)) shown.delete(key);
      else shown.add(key);
      tab.eventCategories = PLOT_EVENTS.map((kind) => kind.key).filter((k) => shown.has(k));
    },

    /** Fetches the events of the visible window of the active tab marked on the plots, from all its logs. */
    async fetchEvents() {
      this.eventsAbort?.abort();
      const kinds = PLOT_EVENTS.filter((kind) => this.activeTab?.eventCategories?.includes(kind.key));
      const sources = kinds.length ? this.eventLogs : [];
      const marks = (this.activeTab?.marks ?? []).filter((m) => m.shown && this.sources[m.source]?.events_status === "ready");
      if ((!sources.length && !marks.length) || !this.view) {
        this.events = [];
        this.eventsTruncated = false;
        return;
      }
      this.eventsAbort = new AbortController();
      const signal = this.eventsAbort.signal;
      try {
        // Whole categories and single types; an empty category list is left out of the query, matching only the types.
        const categories = kinds.filter((kind) => !kind.type).map((kind) => kind.category);
        const types = kinds.filter((kind) => kind.type).map((kind) => kind.type);
        const results = await Promise.all([
          ...sources.map((s) => {
            const shift = this.shifts[s.id];
            const params = { source: s.id, t0: this.view.min - shift, t1: this.view.max - shift, categories, types, limit: MAX_EVENTS };
            return getJSON("/api/events", params, signal).then((res) => ({ res, s, shift }));
          }),
          // All the events of a mark are its lines.
          ...marks.map((m) => {
            const s = this.sources[m.source];
            const shift = this.shifts[s.id];
            const params = { source: s.id, t0: this.view.min - shift, t1: this.view.max - shift, limit: MAX_EVENTS };
            return getJSON("/api/events", params, signal).then((res) => ({ res, s, shift }));
          }),
        ]);
        this.events = results
          .flatMap(({ res, s, shift }) => res.events.map((e) => ({ ...e, t: e.t + shift, source: s.id })))
          .sort((a, b) => a.t - b.t);
        this.eventsTruncated = results.some(({ res }) => res.truncated);
      } catch (e) {
        if (e.name !== "AbortError") this.warnings = [...this.warnings, e.message];
      }
    },

    /**
     * Marks the lines of a log that match a regex, as {source, pattern}, as events on the plots of a tab, the active one
     * by default. shown sets whether they are drawn at first.
     */
    async markLines(mark, shown = true, tab = this.activeTab) {
      if (!tab) return;
      let id;
      try {
        ({ id } = await postJSON(`/api/sources/${mark.source}/adhoc`, { key: null, filter: mark.pattern }));
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
        return;
      }
      if (!tab.marks.some((m) => m.source === id)) tab.marks.push({ source: id, pattern: mark.pattern, log: mark.source, shown });
      await this.pollSources();
    },

    /** @param {"plot" | "table" | "trace" | "grid"} kind */
    async addPlot(kind) {
      const tab = this.activeTab;
      if (!tab) return;
      // Tables continue from the last panel; plots start from the default dataset and field of the plot panel.
      const last = tab.plots.findLast((p) => p.dataset);
      const sameSource = kind === "table" && last && this.runSources.some((s) => s.id === last.source);
      const source = sameSource ? last.source : this.defaultSource(this.activeRun, kind)?.id;
      if (source == null) return;
      const dataset = sameSource ? last.dataset : null;
      tab.plots.push(newPlot({ source, dataset, kind }));
      await this.$nextTick();
      [...document.querySelectorAll("main > .panel")].at(-1)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    },

    /**
     * Opens the ad-hoc metric of a key=value field of the lines of a log, as {source, key, filter, split_key}, and
     * shows it in a plot: in plot if given, which then changes to it, else in a new plot.
     */
    async plotAdhoc(spec, plot = null) {
      let id;
      try {
        ({ id } = await postJSON(`/api/sources/${spec.source}/adhoc`, { key: spec.key, filter: spec.filter ?? null, split_key: spec.split_key ?? null }));
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
        return;
      }
      await this.pollSources();
      const init = { source: id, dataset: "adhoc", field: spec.key, splitBy: spec.split_key ?? null, splitValues: [] };
      if (plot) {
        Object.assign(plot, init);
        return;
      }
      const tab = this.activeTab;
      if (!tab) return;
      tab.plots.push(newPlot({ ...init, kind: "plot" }));
      await this.$nextTick();
      [...document.querySelectorAll("main > .panel")].at(-1)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    },

    removePlot(id) {
      this.activeTab.plots = this.activeTab.plots.filter((p) => p.id !== id);
      if (this.activeTab.focused === id) this.activeTab.focused = null;
    },

    /** Maximizes a panel of the active tab, or restores it when it is maximized. */
    async toggleFocus(id) {
      const tab = this.activeTab;
      tab.focused = tab.focused === id ? null : id;
      await this.$nextTick();
      this.observeHeights();
      if (tab.focused != null) window.scrollTo({ top: 0 });
    },

    /** Watches the elements whose heights a maximized panel leaves to them: the app bar and the log pane. */
    observeHeights() {
      this.heightObserver.disconnect();
      for (const el of document.querySelectorAll(".app-bar, aside.records")) this.heightObserver.observe(el);
      this.updateMaximizedHeight();
    },

    updateMaximizedHeight() {
      const bar = document.querySelector(".app-bar")?.offsetHeight ?? 0;
      const pane = document.querySelector("aside.records")?.offsetHeight ?? 0;
      // The vertical padding of main.
      this.maximizedHeight = Math.max(200, window.innerHeight - bar - pane - 24);
    },

    /** Marks a record of the log pane on the panels, panning the view to it when it is out of the view. */
    focusRecord({ source, record, t }) {
      const shown = t + (this.shifts[source] ?? 0);
      this.focus = { source, record, t: shown };
      const view = this.view;
      if (view && (shown < view.min || shown > view.max)) {
        const half = (view.max - view.min) / 2;
        this.zoom({ min: shown - half, max: shown + half });
      }
    },

    /**
     * Opens a record in the log pane: in the tab that shows it, else in the preview tab, which the next record replaces
     * until it is kept. A log has one tab per source, which shows any of its lines.
     * @param {{source: number, record: number, item?: any, field?: string}} sel
     */
    openRecord(sel, kind = "record", tab = this.activeTab) {
      if (!tab) return null;
      const pane = tab.pane;
      const sel1 = { source: sel.source, record: sel.record, item: sel.item ?? null, field: sel.field ?? null };
      const log = !this.sources[sel.source]?.has_detail;
      let t = pane.tabs.find((t) => t.kind === kind && t.sel.source === sel.source && (log || t.sel.record === sel.record));
      if (t) {
        t.sel = sel1;
      } else {
        t = { id: nextPaneTabId++, kind, sel: sel1, preview: true, title: null, used: 0 };
        const preview = pane.tabs.findIndex((x) => x.preview);
        if (preview >= 0) pane.tabs.splice(preview, 1, t);
        else pane.tabs.push(t);
        if (pane.tabs.length > MAX_PANE_TABS) {
          // By id, since the tabs of the pane are reactive proxies of the objects pushed.
          const lru = pane.tabs.filter((x) => x.id !== t.id).reduce((a, b) => (b.used < a.used ? b : a));
          pane.tabs.splice(pane.tabs.indexOf(lru), 1);
        }
        t = pane.tabs.find((x) => x.id === t.id);
      }
      this.activatePaneTab(t.id, tab);
      return t;
    },

    activatePaneTab(id, tab = this.activeTab) {
      tab.pane.active = id;
      const t = tab.pane.tabs.find((x) => x.id === id);
      if (t) t.used = ++paneTabUses;
    },

    /** Closes a tab of the log pane, showing the one shown last before it. */
    closePaneTab(id) {
      const pane = this.activeTab.pane;
      pane.tabs = pane.tabs.filter((t) => t.id !== id);
      if (pane.active !== id) return;
      pane.active = null;
      this.focus = null;
      const last = pane.tabs.reduce((a, b) => (!a || b.used > a.used ? b : a), null);
      if (last) this.activatePaneTab(last.id);
    },

    closePane() {
      this.activeTab.pane = { tabs: [], active: null };
      this.focus = null;
    },

    /** Keeps a preview tab of the log pane. */
    pinPaneTab(id) {
      const t = this.paneTabs.find((x) => x.id === id);
      if (t) t.preview = false;
    },

    setPaneTitle({ id, title }) {
      const t = this.paneTabs.find((x) => x.id === id);
      if (t) t.title = title;
    },

    zoom(range) {
      if (this.activeTab) this.activeTab.userRange = range && range.max > range.min ? range : null;
    },

    /** Zooms with w and s and pans with a and d, unless typing or a dialog is open. */
    onKey(e) {
      if (e.key === "Escape" && this.activeTab?.focused != null && !this.browserOpen) {
        this.toggleFocus(this.activeTab.focused);
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey || this.browserOpen || this.relatedQuestion) return;
      if (e.target.closest?.("input, select, textarea, [contenteditable]")) return;
      const action = { w: 1 / KEY_ZOOM_FACTOR, s: KEY_ZOOM_FACTOR, a: -KEY_PAN_FRACTION, d: KEY_PAN_FRACTION }[e.key.toLowerCase()];
      if (action == null || !this.view || !this.fullRange) return;
      e.preventDefault();
      const { min, max } = this.view;
      const width = max - min;
      const full = this.fullRange;
      let range;
      if (e.key.toLowerCase() === "w" || e.key.toLowerCase() === "s") {
        const center = (min + max) / 2;
        const half = (width * action) / 2;
        range = { min: center - half, max: center + half };
      } else {
        range = { min: min + width * action, max: max + width * action };
      }
      // The view stays within the time range of the files: a zoom out is cut to it, a pan stops at its ends.
      if (range.max - range.min >= full.max - full.min) {
        this.zoom(null);
        return;
      }
      const shift = Math.max(0, full.min - range.min) - Math.max(0, range.max - full.max);
      this.zoom({ min: range.min + shift, max: range.max + shift });
    },

    /**
     * Opens a file or a directory in its own tab and selects it. A run that is already open just gets its tab selected.
     * @param {string} path
     * @param {boolean} [fromRecent] Whether the path comes from the recent files, which drop it if it fails.
     */
    async openPath(path, fromRecent = false) {
      this.browserOpen = false;
      let run;
      try {
        run = await postJSON("/api/runs", { path });
      } catch (e) {
        if (fromRecent) removeRecent(path);
        this.warnings = [...this.warnings, fromRecent ? `${e.message} It was removed from the recent files.` : e.message];
        return;
      }
      await this.pollSources();
      this.activeId = run.id;
    },

    async closeTab(id) {
      try {
        await fetch(`/api/runs/${id}`, { method: "DELETE" });
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
      }
      const order = this.runs.map((r) => r.id);
      const next = order[order.indexOf(id) + 1] ?? order[order.indexOf(id) - 1] ?? null;
      if (this.activeId === id) this.activeId = next;
      await this.pollSources();
    },

    /** Shows or hides the supported files of the directory of the active run. */
    async toggleRunFiles() {
      if (this.runFiles) {
        this.runFiles = null;
        return;
      }
      try {
        this.runFiles = await getJSON(`/api/runs/${this.activeId}/files`);
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
      }
    },

    /** @param {string} path */
    async addToRun(path) {
      this.runFiles = null;
      try {
        await postJSON(`/api/runs/${this.activeId}/sources`, { path });
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
      }
      await this.pollSources();
    },

    /** Parses a source of the active run left to parse on request. */
    async parseSource(sourceId) {
      try {
        await postJSON(`/api/sources/${sourceId}/parse`, {});
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
      }
      await this.pollSources();
    },

    /** @param {number | null} bytes */
    formatSize(bytes) {
      if (bytes == null) return "";
      const units = ["B", "KB", "MB", "GB"];
      let i = 0;
      while (bytes >= 1024 && i < units.length - 1) {
        bytes /= 1024;
        i++;
      }
      return `${bytes.toFixed(i && bytes < 10 ? 1 : 0)} ${units[i]}`;
    },

    /** Removes a source from the active run, with its plots. */
    async removeFromRun(sourceId) {
      this.runFiles = null;
      try {
        await fetch(`/api/runs/${this.activeId}/sources/${sourceId}`, { method: "DELETE" });
      } catch (e) {
        this.warnings = [...this.warnings, e.message];
      }
      this.activeTab.plots = this.activeTab.plots.filter((p) => p.source !== sourceId);
      for (const t of this.paneTabs.filter((t) => t.sel.source === sourceId)) this.closePaneTab(t.id);
      await this.pollSources();
    },

    /** Refreshes the runs and sources, and again shortly after while some sources or their events are being parsed. */
    async pollSources() {
      clearTimeout(this.pollTimer);
      try {
        const [runs, sources] = await Promise.all([getJSON("/api/runs"), getJSON("/api/sources")]);
        this.runs = runs;
        this.sources = sources;
      } catch (e) {
        this.error = e.message;
        return;
      }
      for (const s of this.sources) {
        if (s.status === "error" && !this.reportedErrors.has(s.id)) {
          this.reportedErrors.add(s.id);
          this.warnings = [...this.warnings, s.error];
        }
      }
      this.syncTabs();
      // All the sources, which include the ad-hoc metrics of the logs of the runs.
      if (this.sources.some(isParsing)) {
        this.pollTimer = setTimeout(() => this.pollSources(), SOURCES_POLL_MS);
      }
    },
  },
  template: `
    <header class="app-bar">
      <img src="img/ocudu_color.png" alt="OCUDU" class="logo" />
      <nav class="tabs" role="tablist">
        <div v-for="r in runs" :key="r.id" role="tab" :aria-selected="r.id === activeId"
             :class="['tab', { active: r.id === activeId }]" :title="r.path"
             @click="activeId = r.id">
          <span>{{ r.name }}</span>
          <span v-if="progress(r) != null" class="muted">{{ Math.floor(progress(r) * 100) }}%</span>
          <button class="icon tab-close" title="Close the tab" @click.stop="closeTab(r.id)">✕</button>
        </div>
        <button class="tab tab-new" :disabled="!canOpen" title="Open a file or a directory in a new tab" @click="browserOpen = true">+</button>
      </nav>
      <div class="spacer"></div>
      <label class="inline">time
        <select v-model="timeMode">
          <option value="absolute">absolute</option>
          <option value="relative">relative</option>
        </select>
      </label>
      <label class="inline">theme
        <select v-model="themePref">
          <option value="auto">auto</option>
          <option value="light">light</option>
          <option value="dark">dark</option>
        </select>
      </label>
      <button @click="zoom(null)" :disabled="!activeTab || !activeTab.userRange" title="Show the whole time range of the files">reset zoom</button>
      <button @click="zoom(panelsRange)" :disabled="!panelsRange" title="Fit the time range of the data in the plots, tables and traces">fit plots</button>
    </header>
    <main :class="{ focused: activeTab?.focused != null }">
      <p v-if="error" class="error">{{ error }}</p>
      <p v-for="w in warnings" :key="w" class="error">{{ w }}</p>
      <div v-if="!runs.length && !error" class="empty-state">
        <p>Nothing is open yet.</p>
        <button @click="browserOpen = true" :disabled="!canOpen">Open a file or a directory</button>
      </div>
      <template v-else-if="activeRun">
        <div class="source-bar">
          <span class="muted">files</span>
          <span v-for="s in runSources" :key="s.id" :class="['source-chip', s.status]" :title="s.status === 'error' ? s.error : s.path">
            {{ s.file }}
            <span v-if="s.status === 'parsing'" class="muted">{{ Math.floor(s.progress * 100) }}%</span>
            <span v-else-if="s.status === 'error'">failed</span>
            <button v-else-if="s.status === 'deferred'" title="Parse this file, left out of the run since such captures can be huge" @click="parseSource(s.id)">parse {{ formatSize(s.size) }}</button>
            <button v-if="runSources.length > 1" class="icon" title="Remove the file from this tab" @click="removeFromRun(s.id)">✕</button>
          </span>
          <button v-if="canOpen" title="Add a file of the same directory to this tab" @click="toggleRunFiles">+ file</button>
          <span v-if="runFiles" class="run-files">
            <span v-if="!runFiles.some((f) => !f.in_run)" class="muted">no other supported files in {{ activeRun.kind === "dir" ? activeRun.path : "the directory" }}</span>
            <button v-for="f in runFiles.filter((f) => !f.in_run)" :key="f.path" :title="f.path" @click="addToRun(f.path)">{{ f.name }}</button>
          </span>
        </div>
        <div v-if="!readySources.length" class="empty-state">
          <p v-if="runProgress != null">Parsing: {{ Math.floor(runProgress * 100) }}%</p>
          <p v-else class="error">None of the files could be parsed.</p>
          <p class="muted">{{ activeRun.path }}</p>
        </div>
        <template v-else-if="activeTab">
          <p class="hint muted">Drag to zoom, Shift+drag to pan, double-click to reset, W/S to zoom in and out, A/D to pan, click a point or an event marker to see its log line or frame.</p>
          <div v-if="eventCategoryChips.length || eventsState || activeNotes.length || activeTab.marks.length" class="event-bar">
            <span class="muted">events</span>
            <span v-if="eventsState === 'parsing'" class="muted">parsing…</span>
            <span v-else-if="eventsState === 'error'" class="error">could not be parsed</span>
            <button v-for="c in eventCategoryChips" :key="c.key" :disabled="!c.count"
                    :class="['event-chip', 'ev-' + c.category, { off: !c.count || !(activeTab.eventCategories || []).includes(c.key) }]"
                    :title="c.count ? 'Show or hide the ' + c.label + ' on the plots' : 'The logs of this tab have no ' + c.label"
                    @click="toggleEventCategory(c.key)">
              <span class="event-dot"></span>{{ c.label }} <span class="muted">{{ c.count }}</span>
            </button>
            <span v-for="(m, i) in activeTab.marks" :key="m.source" class="mark-chip">
              <button :class="['event-chip', 'ev-adhoc', { off: !m.shown }]" :title="'Show or hide the lines matching /' + m.pattern + '/ on the plots'"
                      @click="m.shown = !m.shown">
                <span class="event-dot"></span>“{{ m.pattern }}” <span class="muted">{{ sources[m.source]?.status === "ready" && sources[m.source]?.events_status === "ready" ? sources[m.source].event_counts?.adhoc ?? 0 : "…" }}</span>
              </button>
              <button class="icon" title="Remove this mark" @click="activeTab.marks.splice(i, 1)">✕</button>
            </span>
            <span v-if="eventsTruncated" class="muted">only the first events of the window are shown, zoom in for all</span>
            <span v-for="n in activeNotes" :key="n" class="muted event-note">{{ n }}</span>
          </div>
          <template v-for="p in activeTab.plots" :key="p.id">
            <trace-panel v-if="p.kind === 'trace'" :focus="focus" v-show="activeTab.focused == null || activeTab.focused === p.id" :maximized="activeTab.focused === p.id"
                         :maximized-height="maximizedHeight" @maximize="toggleFocus(p.id)" :panel="p" :sources="sources" :choices="runSources" :run-id="activeId" :view="view" :shifts="shifts"
                         :time-mode="timeMode" :theme-version="themeVersion" @zoom="zoom" @remove="removePlot(p.id)"
                         @select-record="openRecord($event)" />
            <grid-panel v-else-if="p.kind === 'grid'" :focus="focus" v-show="activeTab.focused == null || activeTab.focused === p.id" :maximized="activeTab.focused === p.id"
                         :maximized-height="maximizedHeight" @maximize="toggleFocus(p.id)" :panel="p" :sources="sources" :choices="runSources" :view="view" :shifts="shifts"
                        :time-mode="timeMode" :theme-version="themeVersion" @zoom="zoom" @remove="removePlot(p.id)"
                        @select-record="openRecord($event)" @parse-source="parseSource" />
            <table-panel v-else-if="p.kind === 'table'" v-show="activeTab.focused == null || activeTab.focused === p.id" :maximized="activeTab.focused === p.id"
                         :maximized-height="maximizedHeight" @maximize="toggleFocus(p.id)" :panel="p" :sources="sources" :choices="panelSources" :view="view" :shifts="shifts"
                         :show-source="panelSources.length > 1" @remove="removePlot(p.id)" @select-record="openRecord($event)"
                         @parse-source="parseSource" />
            <plot-panel v-else :focus="focus" v-show="activeTab.focused == null || activeTab.focused === p.id" :maximized="activeTab.focused === p.id"
                         :maximized-height="maximizedHeight" @maximize="toggleFocus(p.id)" :plot="p" :sources="sources" :choices="panelSources" :view="view" :shifts="shifts" :time-mode="timeMode"
                        :theme-version="themeVersion" :show-source="panelSources.length > 1" :events="events"
                        @zoom="zoom" @remove="removePlot(p.id)" @select-record="openRecord($event)" @parse-source="parseSource" @adhoc="plotAdhoc($event, p)" />
          </template>
          <div class="add-buttons">
            <button class="add-plot" title="Plot of a metric over time, or its histogram" @click="addPlot('plot')">+ plot</button>
            <button class="add-plot" title="Table of all the metrics of a layer" @click="addPlot('table')">+ table</button>
            <button class="add-plot" title="Timeline of each UE, with its events" @click="addPlot('trace')">+ trace</button>
            <button v-if="runSources.some((s) => s.type === 'schedtrace')" class="add-plot"
                    title="Resource grid of the cell of a scheduler event trace: its occupancy, and its allocations when zoomed in"
                    @click="addPlot('grid')">+ grid</button>
          </div>
        </template>
      </template>
    </main>
    <record-pane v-if="paneTabs.length" :tabs="paneTabs" :active="activeTab.pane.active" :sources="sources"
                 @close="closePane" @activate="activatePaneTab($event)" @close-tab="closePaneTab" @pin="pinPaneTab" @title="setPaneTitle"
                 @focus="focusRecord" @select-record="openRecord($event)" @plot-key="plotAdhoc($event)" @mark-lines="markLines($event)" />
    <file-browser v-if="browserOpen" :roots="roots" @open="openPath" @close="browserOpen = false" />
    <div v-if="relatedQuestion" class="modal-backdrop">
      <section class="modal run-question" role="alertdialog" aria-labelledby="run-question-title" @keydown.esc="activeTab.relatedAnswered = true">
        <header class="panel-bar"><strong id="run-question-title">Open the whole run?</strong></header>
        <div class="run-question-body">
          <p>The following OCUDU artifacts were produced by the same run of <strong>{{ runSources[0]?.file }}</strong>:</p>
          <ul>
            <li v-for="f in activeTab.related" :key="f.path"><strong>{{ f.name }}</strong></li>
          </ul>
          <p class="muted">{{ activeRun.path.slice(0, activeRun.path.lastIndexOf("/")) }}</p>
          <div class="run-question-actions">
            <button ref="openRunButton" class="primary" @click="promoteRun">open the run in this tab</button>
            <button @click="activeTab.relatedAnswered = true">only this file</button>
          </div>
        </div>
      </section>
    </div>
  `,
};

createApp(App).mount("#app");
