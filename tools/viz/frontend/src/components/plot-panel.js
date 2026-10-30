// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import uPlot from "uplot";
import { buildQuery, getJSON } from "../api.js";
import ExportDialog from "./export-dialog.js";
import { displayUnit, formatStat } from "../units.js";
import { CURSOR_SYNC_KEY, MAX_LEGEND_SERIES, TIME_TICK_SPACE, attachZoomPan, capLegend, chartSeries, cssVar, drawFocus, eventText, fitHeight, fmtFull, formatTime, logOf, plotData, selectToZoom, seriesColor, timeTicks, utcDate, valueAxes } from "./chart-utils.js";

// Distance in pixels within which the cursor is on an event marker.
const EVENT_HIT_PX = 5;
const CHART_HEIGHT = 260;
const steppedPath = uPlot.paths.stepped({ align: 1 });
// Delay before fetching after the view changes, to coalesce pan events.
const FETCH_DELAY_MS = 120;
// Split values listed in the split value picker.
const MAX_LISTED_SPLITS = 300;
// Datasets and fields of new plots, the first that their source has: of logs, then of scheduler event traces.
const DEFAULT_DATASETS = ["sched", "ue_metrics"];
const DEFAULT_FIELDS = ["total_dl_brate", "dl_brate"];
// Field each dataset is split by when picked, e.g. one series per UE.
const DEFAULT_SPLITS = {
  sched: "pci", sched_ue: "rnti", ue_metrics: "rnti", dl_grants: "rnti", ul_grants: "rnti", harq_acks: "rnti", crcs: "rnti",
  csi_reports: "rnti",
};


/**
 * Index of the nearest non-null entry of arr to idx, or -1.
 * @param {Array<any>} arr
 * @param {number} idx
 */
function nearestDefined(arr, idx) {
  for (let d = 0; d < arr.length; d++) {
    if (arr[idx - d] != null) return idx - d;
    if (arr[idx + d] != null) return idx + d;
  }
  return -1;
}

export default {
  name: "PlotPanel",
  props: {
    plot: { type: Object, required: true },
    // Record highlighted in the log pane, as {source, record, t} in display time.
    focus: { type: Object, default: null },
    sources: { type: Array, required: true },
    view: { type: Object, default: null },
    shifts: { type: Array, required: true },
    timeMode: { type: String, required: true },
    themeVersion: { type: Number, default: 0 },
    // Whether the panel shows a source selector, which is not needed when the source is fixed, e.g. by a tab.
    showSource: { type: Boolean, default: true },
    // Sources the panel may show, e.g. the sources of its tab, or all sources when null.
    choices: { type: Array, default: null },
    // Events drawn as markers on time series, in display time, each with t, source, record, category, type and text.
    events: { type: Array, default: () => [] },
    // Whether the panel is maximized, alone with the log pane, and the height it then takes, in CSS pixels.
    maximized: { type: Boolean, default: false },
    maximizedHeight: { type: Number, default: 0 },
  },
  components: { ExportDialog },
  emits: ["zoom", "remove", "select-record", "parse-source", "maximize", "adhoc"],
  data() {
    return {
      maxLegendSeries: MAX_LEGEND_SERIES,
      // Height of the chart, in CSS pixels, larger when the panel is maximized.
      chartHeight: CHART_HEIGHT,
      loading: false,
      error: "",
      downsampled: false,
      totalSplits: 0,
      nofSeries: 0,
      splitOptions: [],
      instanceOptions: [],
      splitFilter: "",
      filterDraft: this.plot.filter,
      // Line regex and split key of an ad-hoc metric being edited, applied as a new ad-hoc metric.
      adhocFilter: "",
      adhocSplit: "",
      stats: [],
      statsSampled: false,
      unit: { divisor: 1, label: "" },
      // Unit of the second metric when it has an axis of its own, else null.
      unit2: null,
      labels: [],
      cursorText: "",
      // Event under the cursor, shown with the cursor readout.
      cursorEvent: null,
      exporting: false,
    };
  },
  computed: {
    /** Sources the panel offers, parsed or not. */
    sourceChoices() {
      return this.choices ?? this.sources;
    },
    source() {
      return this.sources[this.plot.source];
    },
    datasets() {
      return this.source ? this.source.datasets.filter((d) => d.fields.some((f) => f.type === "number")) : [];
    },
    dataset() {
      return this.datasets.find((d) => d.name === this.plot.dataset);
    },
    numericFields() {
      return this.dataset ? this.dataset.fields.filter((f) => f.type === "number" && !this.dataset.context.includes(f.name)) : [];
    },
    /** Field of the second metric when it is shown, which only time series do, else null. */
    field2() {
      return this.plot.second && this.plot.mode !== "histogram" && this.plot.field2 && this.plot.field2 !== this.plot.field ? this.plot.field2 : null;
    },
    splitFields() {
      return this.dataset ? this.dataset.context : [];
    },
    shift() {
      return this.shifts[this.plot.source] || 0;
    },
    listedSplits() {
      const filter = this.splitFilter.trim();
      const values = filter ? this.splitOptions.filter((v) => String(v).includes(filter)) : this.splitOptions;
      return values.slice(0, MAX_LISTED_SPLITS);
    },
    statsRows() {
      return this.stats.map((s) => {
        const info = this.seriesInfo.find((i) => i.label === s.label);
        // Read after a theme change, which changes the series colors.
        void this.themeVersion;
        const color = info ? seriesColor(info.color) : "transparent";
        const divisor = (info?.scale === "y2" ? this.unit2 : this.unit).divisor;
        const scale = (v) => formatStat(v == null ? v : v / divisor);
        const unit = info?.scale === "y2" ? this.unit2.label : this.unit.label;
        return { label: s.label, unit, color, count: s.count.toLocaleString(), min: scale(s.min), mean: scale(s.mean), p50: scale(s.p50), p95: scale(s.p95), p99: scale(s.p99), max: scale(s.max) };
      });
    },
    /** CSV of the samples of the plot in the visible window, at full resolution. */
    csvHref() {
      return `/api/series.csv?${buildQuery({ ...this.queryParams(), field2: this.field2 })}`;
    },
    filterDirty() {
      return this.filterDraft.trim() !== this.plot.filter;
    },
    splitSummary() {
      if (this.plot.splitValues.length) return `${this.plot.splitValues.length} selected`;
      if (this.totalSplits > this.nofSeries) return `first ${this.nofSeries} of ${this.totalSplits}`;
      return "all";
    },
  },
  watch: {
    // The edited spec starts as the one of the ad-hoc metric shown.
    "source.adhoc": {
      handler(spec) {
        this.adhocFilter = spec?.filter ?? "";
        this.adhocSplit = spec?.split_key ?? "";
      },
      immediate: true,
    },
    // A source left to parse on request, e.g. a MAC pcap, is parsed once picked; its datasets come once parsed.
    "source.status": {
      handler(status) {
        if (status === "deferred") this.$emit("parse-source", this.source.id);
      },
      immediate: true,
    },
    datasets(datasets) {
      if (datasets.length && !datasets.some((d) => d.name === this.plot.dataset)) this.plot.dataset = this.defaultDataset();
    },
    "plot.source"() {
      const dataset = this.defaultDataset();
      // An unchanged dataset name does not trigger its watcher, but the instances differ per source.
      if (dataset === this.plot.dataset) this.loadInstanceOptions(true);
      this.plot.dataset = dataset;
    },
    "plot.dataset"() {
      this.plot.field = this.defaultField();
      this.plot.field2 = this.defaultField2();
      // An ad-hoc metric is split by its split key.
      const split = this.source?.adhoc ? this.source.adhoc.split_key : DEFAULT_SPLITS[this.plot.dataset];
      this.plot.splitBy = split && this.splitFields.includes(split) ? split : null;
      this.loadInstanceOptions(true);
    },
    maximized() {
      this.fitChart();
    },
    maximizedHeight() {
      this.fitChart();
    },
    "plot.field"() {
      // Until the second metric is shown, its field follows the first, e.g. ul_brate for dl_brate.
      if (!this.plot.second) this.plot.field2 = this.defaultField2();
    },
    "plot.splitBy"() {
      this.plot.splitValues = [];
      this.splitFilter = "";
      this.loadSplitOptions();
    },
    view: { handler() { this.applyView(); this.scheduleFetch(false); }, deep: true },
    events() {
      // Markers are drawn by the chart, which only needs a redraw.
      this.chart?.redraw(false, false);
    },
    focus() {
      this.chart?.redraw(false, false);
    },
    themeVersion() {
      // Charts take their colors at creation, so they are rebuilt from the last response.
      if (!this.lastRes) return;
      this.rebuildPending = true;
      if (this.lastRes.edges) this.renderHistogram(this.lastRes);
      else this.render(this.lastRes, this.lastRes2, this.lastField2);
    },
  },
  created() {
    // Not reactive: chart state is owned by uPlot.
    this.chart = null;
    this.records = [];
    // How each value series is drawn, see plotData().
    this.seriesInfo = [];
    this.focusedSeries = -1;
    this.lastRes = null;
    this.hovering = false;
    this.fetchTimer = null;
    this.abort = null;
    // Any change of the query inputs refetches and rebuilds the chart.
    this.$watch(
      () => [this.plot.source, this.plot.dataset, this.plot.field, this.field2, this.plot.splitBy, [...this.plot.splitValues], this.plot.filter, this.plot.instance, this.plot.mode, this.shift, this.timeMode],
      () => this.scheduleFetch(true),
    );
  },
  mounted() {
    this.resizeObserver = new ResizeObserver(() => {
      if (this.chart) this.chart.setSize({ width: this.$refs.chart.clientWidth, height: this.chartHeight });
    });
    this.resizeObserver.observe(this.$refs.chart);
    if (!this.plot.dataset) this.plot.dataset = this.defaultDataset();
    if (!this.plot.field) this.plot.field = this.defaultField();
    if (!this.plot.field2) this.plot.field2 = this.defaultField2();
    this.loadInstanceOptions(this.plot.instance == null);
    this.scheduleFetch(true);
  },
  beforeUnmount() {
    this.resizeObserver.disconnect();
    clearTimeout(this.fetchTimer);
    this.abort?.abort();
    this.chart?.destroy();
  },
  methods: {
    eventText,

    /** The first of DEFAULT_DATASETS that the source has, else its first dataset. */
    defaultDataset() {
      const preferred = DEFAULT_DATASETS.map((name) => this.datasets.find((d) => d.name === name)).find(Boolean);
      return (preferred ?? this.datasets[0])?.name ?? null;
    },

    /** The first of DEFAULT_FIELDS that the dataset has, else its first numeric field. */
    defaultField() {
      const preferred = DEFAULT_FIELDS.map((name) => this.numericFields.find((f) => f.name === name)).find(Boolean);
      return (preferred ?? this.numericFields[0])?.name ?? null;
    },

    /** Field of the second metric when first enabled: the uplink one of a downlink field and the other way round, e.g.
     * ul_brate for dl_brate, else the next numeric field. */
    defaultField2() {
      const names = this.numericFields.map((f) => f.name).filter((n) => n !== this.plot.field);
      const field = this.plot.field ?? "";
      const swapped = /(^|_)dl(_|$)/.test(field) ? field.replace(/(^|_)dl(_|$)/, "$1ul$2") : field.replace(/(^|_)ul(_|$)/, "$1dl$2");
      if (swapped !== field && names.includes(swapped)) return swapped;
      const next = this.numericFields.findIndex((f) => f.name === field) + 1;
      return this.numericFields[next]?.name ?? names[0] ?? null;
    },

    /** Enables the second metric, picking a field for it unless one is picked already. */
    toggleSecond(on) {
      if (on && (!this.plot.field2 || this.plot.field2 === this.plot.field)) this.plot.field2 = this.defaultField2();
      this.plot.second = on;
    },

    async loadSplitOptions() {
      this.splitOptions = [];
      if (!this.plot.splitBy) return;
      try {
        this.splitOptions = await getJSON("/api/context", { source: this.plot.source, dataset: this.plot.dataset, field: this.plot.splitBy });
      } catch (e) {
        this.error = e.message;
      }
    },

    /** @param {boolean} selectFirst Whether to select the first instance, e.g. after a dataset change. */
    async loadInstanceOptions(selectFirst) {
      this.instanceOptions = [];
      if (!this.dataset?.instance) {
        this.plot.instance = null;
        return;
      }
      const dataset = this.plot.dataset;
      try {
        const values = await getJSON("/api/context", { source: this.plot.source, dataset, field: this.dataset.instance });
        // The dataset may have changed while the request was in flight.
        if (dataset !== this.plot.dataset) return;
        this.instanceOptions = values.map(String);
        if (selectFirst) this.plot.instance = this.instanceOptions[0] ?? null;
      } catch (e) {
        this.error = e.message;
      }
    },

    /** Shows the ad-hoc metric of the same key with the line regex and split key being edited. */
    applyAdhoc() {
      const spec = this.source?.adhoc;
      if (!spec) return;
      const filter = this.adhocFilter.trim() || null;
      const split = this.adhocSplit.trim() || null;
      if (filter === spec.filter && split === spec.split_key) return;
      this.$emit("adhoc", { source: this.source.parent, key: spec.key, filter, split_key: split });
    },

    applyFilter() {
      this.plot.filter = this.filterDraft.trim();
    },

    toggleSplit(value) {
      const key = String(value);
      const idx = this.plot.splitValues.indexOf(key);
      if (idx >= 0) this.plot.splitValues.splice(idx, 1);
      else this.plot.splitValues.push(key);
    },

    /** @param {boolean} rebuild Whether the series set may have changed. */
    scheduleFetch(rebuild) {
      this.rebuildPending = this.rebuildPending || rebuild;
      clearTimeout(this.fetchTimer);
      this.fetchTimer = setTimeout(() => this.fetch(), FETCH_DELAY_MS);
    },

    /** Query of the series, histogram and statistics of the plot in the visible window, without the width. */
    queryParams() {
      const params = {
        source: this.plot.source,
        dataset: this.plot.dataset,
        field: this.plot.field,
        split_by: this.plot.splitBy,
        split_values: this.plot.splitValues.length ? this.plot.splitValues : null,
        filter: this.plot.filter || null,
        instance: this.dataset?.instance ? this.plot.instance : null,
      };
      if (this.view) {
        params.t0 = this.view.min - this.shift;
        params.t1 = this.view.max - this.shift;
      }
      return params;
    },

    /** Title of the plot in exported images: its dataset, instance and field, with its unit. */
    exportTitle() {
      const parts = [this.dataset?.label ?? this.plot.dataset, this.plot.instance].filter(Boolean);
      const unit = (label) => (label ? ` [${label}]` : "");
      let fields = this.plot.field + (this.unit2 ? unit(this.unit.label) : "");
      if (this.field2) fields += ` + ${this.field2}${this.unit2 ? unit(this.unit2.label) : ""}`;
      return [...parts, fields].join(" › ") + (this.unit2 ? "" : unit(this.unit.label));
    },

    async fetch() {
      if (!this.plot.dataset || !this.plot.field) return;
      this.abort?.abort();
      this.abort = new AbortController();
      const width = Math.max(100, Math.round(this.$refs.chart.clientWidth));
      const params = this.queryParams();
      this.loading = true;
      try {
        const signal = this.abort.signal;
        const histogram = this.plot.mode === "histogram";
        const field2 = this.field2;
        const params2 = { ...params, field: field2 };
        const [res, stats, res2, stats2] = await Promise.all([
          histogram ? getJSON("/api/histogram", params, signal) : getJSON("/api/series", { ...params, width }, signal),
          getJSON("/api/stats", params, signal),
          field2 ? getJSON("/api/series", { ...params2, width }, signal) : null,
          field2 ? getJSON("/api/stats", params2, signal) : null,
        ]);
        this.error = "";
        if (histogram) this.renderHistogram(res);
        else this.render(res, res2, field2);
        // Labelled like their series, see plotData().
        const named = (f, s) => ({ ...s, label: field2 == null || s.label === f ? s.label : `${f} ${s.label}` });
        this.stats = [...stats.series.map((s) => named(this.plot.field, s)), ...(stats2?.series ?? []).map((s) => named(field2, s))];
        this.statsSampled = stats.sampled || Boolean(stats2?.sampled);
      } catch (e) {
        if (e.name !== "AbortError") this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    render(res, res2 = null, field2 = null) {
      this.lastRes = res;
      this.lastRes2 = res2;
      this.lastField2 = field2;
      this.downsampled = res.downsampled || Boolean(res2?.downsampled);
      this.totalSplits = res.total_splits;
      const { data, series, labels, unit, unit2, records } = plotData(res, res2, this.shift, this.plot.field, field2);
      this.nofSeries = series.length;
      this.records = records;
      this.seriesInfo = series;
      this.unit = unit;
      this.unit2 = unit2;
      this.labels = labels;

      const key = JSON.stringify(["time", series, unit.label, unit2?.label]);
      if (this.chart && !this.rebuildPending && this.chartKey === key) {
        this.chart.setData(data, false);
      } else {
        this.chart?.destroy();
        this.chartKey = key;
        this.chart = this.createChart(data, series, unit, unit2);
      }
      this.rebuildPending = false;
      this.applyView();
    },

    renderHistogram(res) {
      this.lastRes = res;
      this.downsampled = false;
      this.totalSplits = res.total_splits;
      this.nofSeries = res.series.length;
      this.records = [];
      let maxAbs = 0;
      for (const e of res.edges) maxAbs = Math.max(maxAbs, Math.abs(e));
      const unit = displayUnit(res.unit, maxAbs);
      const labels = res.series.map((s) => s.label);
      this.unit = unit;
      this.labels = labels;
      this.unit2 = null;
      this.seriesInfo = labels.map((label, i) => ({ label, color: i, dash: false, scale: "y", field: this.plot.field }));
      // A stepped path draws bin i from edge i to edge i+1, so the last count is repeated at the last edge.
      const data = [
        res.edges.map((e) => e / unit.divisor),
        ...res.series.map((s) => [...s.counts, s.counts[s.counts.length - 1]]),
      ];

      const key = JSON.stringify(["histogram", labels, unit.label]);
      if (this.chart && !this.rebuildPending && this.chartKey === key) {
        this.chart.setData(data);
      } else {
        this.chart?.destroy();
        this.chartKey = key;
        this.chart = this.createHistogramChart(data, labels, unit.label);
      }
      this.rebuildPending = false;
    },

    /** Shows the time and value under the mouse, or only the time when the cursor is synced from another plot. */
    updateCursorReadout(u) {
      const { left, top } = u.cursor;
      if (left == null || left < 0) {
        this.cursorText = "";
        this.cursorEvent = null;
        return;
      }
      this.cursorEvent = this.hovering && this.plot.mode !== "histogram" ? this.eventAt(u, left) : null;
      const histogram = this.plot.mode === "histogram";
      const unit = this.unit.label ? ` ${this.unit.label}` : "";
      const x = u.posToVal(left, "x");
      let text = histogram ? `${formatStat(x)}${unit}` : formatTime(x, this.timeMode);
      if (this.hovering && top != null && top >= 0) {
        const y = u.posToVal(top, "y");
        text += histogram ? ` \u00b7 count ${formatStat(Math.max(0, y))}` : ` \u00b7 ${formatStat(y)}${unit}`;
        // The value of the right axis of the second metric too.
        if (!histogram && this.unit2) text += ` \u00b7 ${formatStat(u.posToVal(top, "y2"))}${this.unit2.label ? ` ${this.unit2.label}` : ""}`;
      }
      this.cursorText = text;
    },

    /** Sizes the chart to fill the panel when maximized, else to its usual height. */
    async fitChart() {
      // Twice, since the first resize can change the height of the rest of the panel, e.g. its legend.
      for (let pass = 0; pass < 2; pass++) {
        await this.$nextTick();
        const height = this.maximized ? fitHeight(this.$el, this.chartHeight, this.maximizedHeight) : CHART_HEIGHT;
        if (height === this.chartHeight) return;
        this.chartHeight = height;
        this.chart?.setSize({ width: this.$refs.chart.clientWidth, height });
      }
    },

    /** Tracks whether the mouse is over this chart, as opposed to a cursor synced from another plot. */
    trackHover(chart) {
      chart.over.addEventListener("mouseenter", () => { this.hovering = true; });
      chart.over.addEventListener("mouseleave", () => {
        this.hovering = false;
        this.cursorText = "";
      });
    },

    applyView() {
      if (this.chart && this.view && this.plot.mode !== "histogram") {
        this.chart.setScale("x", { min: this.view.min, max: this.view.max });
      }
    },

    createHistogramChart(data, labels, unitLabel) {
      const axisColor = cssVar("--fg-muted");
      const gridColor = cssVar("--grid");
      const axis = { stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor, width: 1 } };
      const single = labels.length === 1;
      const opts = {
        width: this.$refs.chart.clientWidth,
        height: this.chartHeight,
        scales: { x: { time: false } },
        series: [
          { label: unitLabel ? `value [${unitLabel}]` : "value", value: (u, v) => (v == null ? "--" : formatStat(v)) },
          ...labels.map((label, i) => {
            const color = seriesColor(i);
            return { label, stroke: color, width: 1.5, paths: steppedPath, points: { show: false }, fill: single ? color + "40" : undefined };
          }),
        ],
        axes: [{ ...axis, label: unitLabel }, { ...axis, label: "count", size: 60 }],
        cursor: { drag: { x: false, y: false }, focus: { prox: 16 } },
        focus: { alpha: 0.35 },
        hooks: { setCursor: [(u) => this.updateCursorReadout(u)] },
      };
      const chart = new uPlot(opts, data, this.$refs.chart);
      capLegend(chart);
      this.trackHover(chart);
      return chart;
    },

    createChart(data, series, unit, unit2) {
      const axisColor = cssVar("--fg-muted");
      const gridColor = cssVar("--grid");
      const axis = { stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor, width: 1 } };
      const absolute = this.timeMode === "absolute";
      const xAxis = absolute ? { ...axis, values: timeTicks, space: TIME_TICK_SPACE } : { ...axis };
      const opts = {
        width: this.$refs.chart.clientWidth,
        height: this.chartHeight,
        scales: { x: { time: absolute } },
        tzDate: utcDate,
        series: [
          absolute
            ? { label: "time (UTC)", value: (u, ts) => (ts == null ? "--" : fmtFull(utcDate(ts))) }
            : { label: "time (s)", value: (u, t) => (t == null ? "--" : t.toFixed(3)) },
          ...chartSeries(series, 1.25),
        ],
        axes: [xAxis, ...valueAxes(axis, unit, unit2, this.plot.field, this.lastField2)],
        cursor: { drag: { x: true, y: false, setScale: false }, sync: { key: CURSOR_SYNC_KEY }, focus: { prox: 16 } },
        focus: { alpha: 0.35 },
        hooks: {
          setSelect: [selectToZoom((range) => this.$emit("zoom", range))],
          setSeries: [(u, idx) => { this.focusedSeries = idx ?? -1; }],
          setCursor: [(u) => this.updateCursorReadout(u)],
          draw: [(u) => this.drawEvents(u), (u) => drawFocus(u, this.focus, this.focusPoints(u))],
        },
      };
      const chart = new uPlot(opts, data, this.$refs.chart);
      capLegend(chart);
      this.trackHover(chart);
      attachZoomPan(chart, (range) => this.$emit("zoom", range), () => this.drillDown(chart));
      return chart;
    },

    /**
     * Event whose marker is nearest to a cursor position, within EVENT_HIT_PX, or null.
     * @param {any} u
     * @param {number} left Cursor position in CSS pixels from the left of the plot area.
     */
    eventAt(u, left) {
      let best = null;
      let bestDist = EVENT_HIT_PX;
      for (const ev of this.events) {
        const d = Math.abs(u.valToPos(ev.t, "x") - left);
        if (d <= bestDist) {
          best = ev;
          bestDist = d;
        }
      }
      return best;
    },

    /**
     * Points of the plot of the focused record, as [x, y, scale key]: those of its series from the same log, e.g. of a
     * plot of one of its ad-hoc metrics.
     */
    focusPoints(u) {
      const focus = this.focus;
      if (!focus || logOf(this.sources, focus.source) !== logOf(this.sources, this.plot.source)) return [];
      const points = [];
      this.records.forEach((records, si) => {
        const idx = records?.indexOf(focus.record) ?? -1;
        if (idx >= 0 && u.data[si + 1]?.[idx] != null) points.push([u.data[0][idx], u.data[si + 1][idx], u.series[si + 1].scale]);
      });
      return points;
    },

    /** Draws the event markers as dashed vertical lines over the plot area, colored by category. */
    drawEvents(u) {
      if (!this.events.length) return;
      const { ctx, bbox } = u;
      const colors = {};
      ctx.save();
      ctx.lineWidth = Math.max(1, devicePixelRatio);
      ctx.globalAlpha = 0.75;
      const dash = 4 * devicePixelRatio;
      for (const ev of this.events) {
        const x = Math.round(u.valToPos(ev.t, "x", true));
        if (x < bbox.left || x > bbox.left + bbox.width) continue;
        ctx.strokeStyle = colors[ev.category] ??= cssVar(`--ev-${ev.category}`);
        ctx.setLineDash([dash, dash]);
        ctx.beginPath();
        ctx.moveTo(x, bbox.top);
        ctx.lineTo(x, bbox.top + bbox.height);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = ctx.strokeStyle;
        ctx.fillRect(x - 2 * devicePixelRatio, bbox.top, 4 * devicePixelRatio, 4 * devicePixelRatio);
      }
      ctx.restore();
    },

    drillDown(chart) {
      // An event marker under the cursor takes precedence over the data points.
      const ev = chart.cursor.left >= 0 ? this.eventAt(chart, chart.cursor.left) : null;
      if (ev) {
        this.$emit("select-record", { source: ev.source ?? this.plot.source, record: ev.record });
        return;
      }
      const idx = chart.cursor.idx;
      if (idx == null || !this.records.length) return;
      let si = this.focusedSeries > 0 ? this.focusedSeries - 1 : this.records.findIndex((r) => r[idx] != null);
      if (si < 0) si = 0;
      const j = nearestDefined(this.records[si], idx);
      if (j >= 0) this.$emit("select-record", { source: this.plot.source, record: this.records[si][j], field: this.seriesInfo[si]?.field ?? this.plot.field });
    },
  },
  template: `
    <section class="panel">
      <header class="panel-bar">
        <template v-if="source?.adhoc">
          <strong class="adhoc-title" :title="source.path">{{ source.file }} › {{ source.adhoc.key }}=</strong>
          <form class="adhoc-bar" @submit.prevent="applyAdhoc">
            <label class="inline" title="Keep the values of the lines that match this regular expression (Python syntax, case-insensitive). Enter applies it.">filter
              <input v-model="adhocFilter" :class="{ dirty: (adhocFilter.trim() || null) !== source.adhoc.filter }" placeholder="regex, e.g. slot decisions" />
            </label>
            <label class="inline" title="One series per value of this key, e.g. ue: each value takes the nearest one before it in its line. Enter applies it.">split by
              <input v-model="adhocSplit" class="adhoc-split" :class="{ dirty: (adhocSplit.trim() || null) !== source.adhoc.split_key }" placeholder="key, e.g. ue" />
            </label>
            <button type="submit" hidden></button>
          </form>
          <select v-model="plot.mode" title="View">
            <option value="time">time series</option>
            <option value="histogram">histogram</option>
          </select>
        </template>
        <template v-else>
          <select v-if="showSource" v-model.number="plot.source" :title="source ? source.path : 'Source'">
            <option v-for="s in sourceChoices" :key="s.id" :value="s.id" :disabled="s.status === 'error'">
              {{ s.adhoc ? s.name : s.file ?? s.name }}{{ s.status === "parsing" ? " (parsing)" : s.status === "deferred" ? " (not parsed)" : s.status === "error" ? " (error)" : "" }}
            </option>
          </select>
          <select v-model="plot.dataset" title="Dataset">
            <option v-for="d in datasets" :key="d.name" :value="d.name">{{ d.label }}</option>
          </select>
          <select v-if="dataset && dataset.instance" v-model="plot.instance" :title="dataset.instance">
            <option :value="null">all</option>
            <option v-for="v in instanceOptions" :key="v" :value="v">{{ v }}</option>
          </select>
          <select v-model="plot.field" title="Field" class="field-select">
            <option v-for="f in numericFields" :key="f.name" :value="f.name">{{ f.name }}</option>
          </select>
          <label v-if="plot.mode !== 'histogram'" class="inline" title="A second field of the dataset, dashed, on its own axis on the right when its unit differs">
            <input type="checkbox" :checked="plot.second" @change="toggleSecond($event.target.checked)" /> 2nd
          </label>
          <select v-if="plot.second && plot.mode !== 'histogram'" v-model="plot.field2" title="Second field" class="field-select">
            <option v-for="f in numericFields" :key="f.name" :value="f.name" :disabled="f.name === plot.field">{{ f.name }}</option>
          </select>
          <select v-model="plot.mode" title="View">
            <option value="time">time series</option>
            <option value="histogram">histogram</option>
          </select>
          <label class="inline">split by
            <select v-model="plot.splitBy">
              <option :value="null">none</option>
              <option v-for="g in splitFields" :key="g" :value="g">{{ g }}</option>
            </select>
          </label>
        </template>
        <details v-if="plot.splitBy" class="splits">
          <summary>{{ splitSummary }}</summary>
          <div class="splits-popup">
            <input v-model="splitFilter" placeholder="filter" />
            <button v-if="plot.splitValues.length" @click="plot.splitValues.splice(0)">clear</button>
            <label v-for="v in listedSplits" :key="v" class="split-option">
              <input type="checkbox" :checked="plot.splitValues.includes(String(v))" @change="toggleSplit(v)" /> {{ v }}
            </label>
            <div v-if="splitOptions.length > listedSplits.length" class="muted">filter to see more</div>
          </div>
        </details>
        <form v-if="!source?.adhoc" class="filter" @submit.prevent="applyFilter">
          <input v-model="filterDraft" :class="{ dirty: filterDirty }" placeholder="rnti > 0x4605 and pci == 1"
                 title="Comparisons (== != < <= > >=, is null) joined by and/or/not. Values in base units (us, bps)." @blur="applyFilter" />
        </form>
        <span class="status">
          <span v-if="loading" class="muted">loading</span>
          <span v-else-if="plot.mode === 'histogram'" class="muted">distribution of the visible window</span>
          <span v-else-if="downsampled" class="muted" title="min/max per pixel; zoom in for full resolution">downsampled</span>
          <span v-if="error" class="error">{{ error }}</span>
        </span>
        <a v-if="labels.length" class="button" :href="csvHref" download
           title="Samples of the plotted series in the visible window, at full resolution, values in the canonical unit">CSV</a>
        <button class="icon" :disabled="plot.mode === 'histogram' || !labels.length" title="Save the plot as an image" @click="exporting = true">💾</button>
        <button class="icon" :title="maximized ? 'Restore (Esc)' : 'Maximize, alone with the log pane'" @click="$emit('maximize')">{{ maximized ? "🗗" : "⛶" }}</button>
        <button class="icon" title="Remove plot" @click="$emit('remove')">✕</button>
      </header>
      <div class="chart-wrap">
        <div ref="chart" class="chart"></div>
        <div v-if="cursorText" class="cursor-readout">
          {{ cursorText }}
          <div v-if="cursorEvent" :class="['cursor-event', 'ev-' + cursorEvent.category]">
            <span class="event-dot"></span>{{ eventText(cursorEvent) }}
          </div>
        </div>
      </div>
      <p v-if="nofSeries > maxLegendSeries" class="muted legend-note">legend and statistics list the first {{ maxLegendSeries }} of {{ nofSeries }} series</p>
      <details v-if="statsRows.length" class="stats" open>
        <summary class="muted">statistics of the visible window{{ !unit2 && unit.label ? " [" + unit.label + "]" : "" }}{{ statsSampled ? ", percentiles sampled" : "" }}</summary>
        <table>
          <thead><tr><th></th><th>count</th><th>min</th><th>mean</th><th>p50</th><th>p95</th><th>p99</th><th>max</th></tr></thead>
          <tbody>
            <tr v-for="r in statsRows" :key="r.label">
              <td><span class="swatch" :style="{ background: r.color }"></span>{{ r.label }}{{ unit2 && r.unit ? " [" + r.unit + "]" : "" }}</td>
              <td>{{ r.count }}</td><td>{{ r.min }}</td><td>{{ r.mean }}</td><td>{{ r.p50 }}</td><td>{{ r.p95 }}</td><td>{{ r.p99 }}</td><td>{{ r.max }}</td>
            </tr>
          </tbody>
        </table>
      </details>
      <export-dialog v-if="exporting" :params="queryParams()" :field2="field2" :shift="shift" :view="view" :time-mode="timeMode"
                     :title="exportTitle()" :name-parts="[source?.file?.replace(/\\.[^.]*$/, ''), plot.dataset, plot.instance, plot.field, field2]"
                     :events="events" @close="exporting = false" />
    </section>
  `,
};
