// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import uPlot from "uplot";
import { getJSON } from "../api.js";
import { CURSOR_SYNC_KEY, TIME_TICK_SPACE, attachZoomPan, cssVar, drawFocus, fitHeight, formatTime, selectToZoom, timeTicks, utcDate } from "./chart-utils.js";

// Height of the chart, in CSS pixels, of one grid (TDD) and of the DL and UL grids (FDD).
const TDD_HEIGHT = 320;
const FDD_HEIGHT = 440;
// Gap between the DL and UL grids of an FDD cell, as a fraction of its CRBs.
const FDD_GAP = 0.08;
const AXIS_SIZE = 70;
// CRBs between the ticks of the CRB axis.
const CRB_TICK = 50;
const FETCH_DELAY_MS = 120;
const SYMBOLS_PER_SLOT = 14;
// Channels in legend order, with their direction.
const CHANNELS = [
  ["PDSCH", "DL"], ["SIB", "DL"], ["RAR", "DL"], ["Paging", "DL"], ["SSB", "DL"], ["CSI-RS", "DL"], ["PDCCH", "DL"],
  ["PUSCH", "UL"], ["PUCCH", "UL"], ["PRACH", "UL"],
];
const UL_CHANNELS = new Set(CHANNELS.filter(([, d]) => d === "UL").map(([c]) => c));
// Slots of the window around a clicked time that a click on the occupancy zooms to.
const ZOOM_SLOTS = 40;
// Minimum pixels between the SFN.slot ticks of the time axis.
const SLOT_TICK_SPACE = 90;
// Frames after which the SFN wraps.
const NOF_SFNS = 1024;

/** @param {string} channel */
function channelColor(channel) {
  return cssVar(`--ch-${channel.toLowerCase()}`);
}

/**
 * A #rrggbb color mixed toward black by a fraction, or the color itself if not #rrggbb.
 * @param {string} color
 * @param {number} amount
 */
function darken(color, amount) {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const channels = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
  return `rgb(${channels.map((c) => Math.round(c * (1 - amount))).join(",")})`;
}

/** @param {number | null | undefined} rnti */
function hexRnti(rnti) {
  return rnti == null ? "" : `0x${rnti.toString(16).padStart(4, "0")}`;
}

// DL PDCCH dci_type values of the common DCIs, of SI-RNTI, RA-RNTI and P-RNTI, which schedule no HARQ process.
const COMMON_DL_DCI_TYPES = new Set([0, 1, 4]);

/** Whether an allocation of the grid belongs to a HARQ process of a UE. */
function isHarqAllocation(item) {
  const list = item.item?.[0];
  if (list === "ue_pdschs" || list === "puschs" || list === "ul_pdcchs") return true;
  return list === "dl_pdcchs" && !COMMON_DL_DCI_TYPES.has(item.dci_type);
}

export default {
  name: "GridPanel",
  props: {
    // Record highlighted in the log pane, as {source, record, t} in display time.
    focus: { type: Object, default: null },
    // Allocations of the HARQ transaction shown in the log pane, as {source, members}, members being "record:list:index".
    outline: { type: Object, default: null },
    panel: { type: Object, required: true },
    sources: { type: Array, required: true },
    // Sources of the tab, among which those with a resource grid.
    choices: { type: Array, default: null },
    view: { type: Object, default: null },
    shifts: { type: Array, required: true },
    timeMode: { type: String, required: true },
    themeVersion: { type: Number, default: 0 },
    // Whether the panel is maximized, alone with the log pane, and the height it then takes, in CSS pixels.
    maximized: { type: Boolean, default: false },
    maximizedHeight: { type: Number, default: 0 },
  },
  emits: ["zoom", "remove", "select-record", "parse-source", "maximize", "inspect-harq"],
  data() {
    // maxChartHeight is the height of the chart that fills the panel when maximized, and menu the context menu of an
    // allocation, at its position in the window.
    return { loading: false, error: "", mode: "", level: 0, nofSlots: 0, cursorText: "", cursorItem: "", maxChartHeight: null, menu: null };
  },
  computed: {
    source() {
      return this.sources[this.panel.source];
    },
    gridChoices() {
      return (this.choices ?? []).filter((s) => s.type === "schedtrace");
    },
    shift() {
      return this.shifts[this.panel.source] || 0;
    },
    cell() {
      return this.source?.grid ?? null;
    },
    fdd() {
      return this.cell?.duplex === "FDD";
    },
    chartHeight() {
      if (this.maximized && this.maxChartHeight) return this.maxChartHeight;
      return this.fdd ? FDD_HEIGHT : TDD_HEIGHT;
    },
    legend() {
      return CHANNELS.map(([name]) => ({ name, color: (void this.themeVersion, channelColor(name)) }));
    },
    status() {
      if (this.mode === "rects") return `${this.nofSlots} slots, allocations`;
      if (this.mode === "cells") return `${this.nofSlots} slots, occupancy ${this.level === 1 ? "per slot" : `per ${this.level} slots`}`;
      return "";
    },
  },
  watch: {
    focus() {
      this.chart?.redraw(false, false);
    },
    outline() {
      this.chart?.redraw(false, false);
    },
    // A trace left to parse on request is parsed once a grid shows it.
    "source.status": {
      handler(status) {
        if (status === "deferred") this.$emit("parse-source", this.source.id);
      },
      immediate: true,
    },
    view: { handler() { this.scheduleFetch(); }, deep: true },
    shift() {
      this.scheduleFetch();
    },
    "panel.source"() {
      this.rebuild();
      this.scheduleFetch();
    },
    cell() {
      this.rebuild();
      this.scheduleFetch();
    },
    timeMode() {
      this.rebuild();
    },
    maximized() {
      this.fitChart();
    },
    maximizedHeight() {
      this.fitChart();
    },
    chartHeight(height) {
      this.chart?.setSize({ width: this.$refs.chart.clientWidth, height });
    },
    themeVersion() {
      this.rebuild();
    },
  },
  created() {
    // Not reactive: the drawing state, owned by the chart.
    this.chart = null;
    this.res = null;
    this.merged = null;
    this.anchorCache = null;
    this.ticksAreSlots = false;
    this.tickCounts = [];
    this.fetchTimer = null;
    this.abort = null;
    this.hovering = false;
  },
  mounted() {
    this.resizeObserver = new ResizeObserver(() => {
      if (this.chart) this.chart.setSize({ width: this.$refs.chart.clientWidth, height: this.chartHeight });
    });
    this.resizeObserver.observe(this.$refs.chart);
    this.scheduleFetch();
    // A press anywhere but on the menu, or Esc, closes it.
    this.pointerListener = (e) => {
      if (this.menu && !this.$refs.menu?.contains(e.target)) this.menu = null;
    };
    this.keyListener = (e) => {
      if (e.key !== "Escape" || !this.menu) return;
      e.stopPropagation();
      this.menu = null;
    };
    window.addEventListener("pointerdown", this.pointerListener, true);
    window.addEventListener("keydown", this.keyListener, true);
  },
  beforeUnmount() {
    window.removeEventListener("pointerdown", this.pointerListener, true);
    window.removeEventListener("keydown", this.keyListener, true);
    this.resizeObserver.disconnect();
    clearTimeout(this.fetchTimer);
    this.abort?.abort();
    this.chart?.destroy();
  },
  methods: {
    /** Sizes the chart to fill the panel when maximized. */
    async fitChart() {
      // Twice, since the first resize can change the height of the rest of the panel, e.g. its header.
      for (let pass = 0; pass < 2; pass++) {
        await this.$nextTick();
        this.maxChartHeight = this.maximized ? fitHeight(this.$el, this.chartHeight, this.maximizedHeight) : null;
      }
    },

    scheduleFetch() {
      clearTimeout(this.fetchTimer);
      this.fetchTimer = setTimeout(() => this.fetch(), FETCH_DELAY_MS);
    },

    async fetch() {
      if (!this.view || !this.cell || this.source?.status !== "ready") return;
      this.abort?.abort();
      this.abort = new AbortController();
      this.loading = true;
      try {
        const width = Math.max(100, Math.round(this.$refs.chart.clientWidth));
        const res = await getJSON(
          "/api/grid",
          { source: this.panel.source, t0: this.view.min - this.shift, t1: this.view.max - this.shift, width },
          this.abort.signal,
        );
        this.error = "";
        this.res = res;
        this.mode = res.mode;
        this.level = res.level ?? 0;
        this.nofSlots = res.nof_slots;
        this.draw();
      } catch (e) {
        if (e.name !== "AbortError") this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    draw() {
      if (!this.chart) {
        this.chart = this.createChart();
        return;
      }
      this.chart.setData([[this.view.min, this.view.max], [null, null]], false);
      this.chart.setScale("x", { min: this.view.min, max: this.view.max });
      this.chart.redraw(false, true);
    },

    rebuild() {
      this.chart?.destroy();
      this.chart = null;
      if (this.view && this.cell) this.draw();
    },

    /** Lowest and highest y of the CRBs of a direction: one grid for TDD, and for FDD the DL grid above the UL one. */
    band(direction) {
      const n = this.cell.nof_crbs;
      if (!this.fdd || direction === "UL") return [0, n];
      const lo = n * (1 + FDD_GAP);
      return [lo, lo + n];
    },

    createChart() {
      const axisColor = cssVar("--fg-muted");
      const gridColor = cssVar("--grid");
      const axis = { stroke: axisColor, grid: { stroke: gridColor, width: 1 }, ticks: { stroke: gridColor, width: 1 } };
      const absolute = this.timeMode === "absolute";
      const n = this.cell.nof_crbs;
      const top = this.fdd ? this.band("DL")[1] : n;
      const ticks = [];
      for (const direction of this.fdd ? ["UL", "DL"] : ["UL"]) {
        const [lo] = this.band(direction);
        for (let crb = 0; crb <= n; crb += CRB_TICK) ticks.push({ y: lo + crb, label: this.fdd ? `${direction} ${crb}` : String(crb) });
      }
      const opts = {
        width: this.$refs.chart.clientWidth,
        height: this.chartHeight,
        scales: { x: { time: absolute }, y: { range: () => [0, top] } },
        tzDate: utcDate,
        legend: { show: false },
        series: [{}, { show: false }],
        axes: [
          // Ticks at slot boundaries labelled SFN.slot, falling back to times until the slots of the window are known.
          {
            ...axis,
            label: "SFN.slot",
            splits: (u, i, min, max) => this.slotTicks(u, min, max) ?? [],
            values: (u, splits) => (this.ticksAreSlots ? this.tickCounts.map((c) => this.countLabel(c)) : absolute ? timeTicks(u, splits) : splits),
            space: TIME_TICK_SPACE,
          },
          { ...axis, size: AXIS_SIZE, label: "CRB", splits: () => ticks.map((t) => t.y), values: () => ticks.map((t) => t.label) },
        ],
        cursor: { drag: { x: true, y: false, setScale: false }, sync: { key: CURSOR_SYNC_KEY }, points: { show: false } },
        hooks: {
          setSelect: [selectToZoom((range) => this.$emit("zoom", range))],
          setCursor: [(u) => this.updateCursor(u)],
          draw: [(u) => this.drawGrid(u), (u) => drawFocus(u, this.focus)],
        },
      };
      const chart = new uPlot(opts, [[this.view.min, this.view.max], [null, null]], this.$refs.chart);
      chart.setScale("x", { min: this.view.min, max: this.view.max });
      chart.over.addEventListener("mouseenter", () => { this.hovering = true; });
      chart.over.addEventListener("mouseleave", () => {
        this.hovering = false;
        this.cursorItem = "";
      });
      attachZoomPan(chart, (range) => this.$emit("zoom", range), () => this.onClick(chart));
      chart.over.addEventListener("contextmenu", (e) => this.onContextMenu(e, chart));
      return chart;
    },

    /** Slot counts of the anchors of the response, unwrapped across SFN wraps, as [display time, count]. */
    anchors() {
      if (this.anchorCache?.res === this.res) return this.anchorCache.list;
      const wrap = NOF_SFNS * this.cell.slots_per_frame;
      let offset = 0;
      let last = null;
      const list = [];
      for (const [t, slotTx] of this.res?.anchors ?? []) {
        if (last != null && slotTx + offset < last - wrap / 2) offset += wrap;
        last = slotTx + offset;
        list.push([t + this.shift, last]);
      }
      this.anchorCache = { res: this.res, list };
      return list;
    },

    /** The anchor nearest a display time, by binary search, or null. */
    nearestAnchor(list, t) {
      if (!list.length) return null;
      let lo = 0;
      let hi = list.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (list[mid][0] < t) lo = mid + 1;
        else hi = mid;
      }
      return lo > 0 && t - list[lo - 1][0] < list[lo][0] - t ? list[lo - 1] : list[lo];
    },

    /** Unwrapped count of the slot that holds a display time, from the nearest anchor and the slot duration, or null. */
    slotAt(t) {
      const a = this.nearestAnchor(this.anchors(), t);
      // The epsilon keeps the start of a slot, e.g. a tick, in that slot despite rounding.
      return a ? a[1] + Math.floor((t - a[0]) / this.cell.slot_duration_s + 1e-6) : null;
    },

    /** SFN.slot of the slot that holds a display time. */
    slotLabel(t) {
      const count = this.slotAt(t);
      return count == null ? "" : this.countLabel(count);
    },

    /** SFN.slot of an unwrapped slot count. */
    countLabel(count) {
      const spf = this.cell.slots_per_frame;
      const slot = ((count % (NOF_SFNS * spf)) + NOF_SFNS * spf) % (NOF_SFNS * spf);
      return `${Math.floor(slot / spf)}.${slot % spf}`;
    },

    /**
     * Ticks of the time axis at the starts of slots of round counts, at least SLOT_TICK_SPACE pixels apart, or null
     * while the slots of the window are unknown.
     */
    slotTicks(u, min, max) {
      const list = this.cell ? this.anchors() : [];
      this.ticksAreSlots = list.length > 0;
      if (!list.length) return null;
      const spf = this.cell.slots_per_frame;
      const duration = this.cell.slot_duration_s;
      const per = ((max - min) / duration) / Math.max(1, u.bbox.width / devicePixelRatio / SLOT_TICK_SPACE);
      // Up to 500 frames: a larger step would land on the same SFNs, which wrap every 1024 frames.
      const steps = [1, 2, 5, 10, spf, 2 * spf, 5 * spf, 10 * spf, 20 * spf, 50 * spf, 100 * spf, 200 * spf, 500 * spf];
      const step = steps.find((s) => s >= per) ?? steps[steps.length - 1];
      const first = this.slotAt(min);
      const last = this.slotAt(max);
      const ticks = [];
      // The slot counts of the ticks, which label them: mapping a tick back to a slot could land on the next one, since
      // the times of the slots jitter around their nominal spacing.
      this.tickCounts = [];
      for (let count = Math.ceil(first / step) * step; count <= last && ticks.length < 200; count += step) {
        const a = this.nearestAnchor(list, min + (count - first) * duration);
        ticks.push(a[0] + (count - a[1]) * duration);
        this.tickCounts.push(count);
      }
      return ticks;
    },

    /** Display times of the symbols [first, first + count) of the slot at source time t. */
    symbolSpan(t, first, count) {
      const symbol = this.cell.slot_duration_s / SYMBOLS_PER_SLOT;
      return [t + this.shift + first * symbol, t + this.shift + (first + count) * symbol];
    },

    /** Canvas rectangle of times [t0, t1) and CRBs [crb, crb + count) of a direction, or null when out of view. */
    box(u, t0, t1, direction, crb, count) {
      const [lo] = this.band(direction);
      const x0 = u.valToPos(t0, "x", true);
      const x1 = u.valToPos(t1, "x", true);
      const y0 = u.valToPos(lo + crb + count, "y", true);
      const y1 = u.valToPos(lo + crb, "y", true);
      const { left, width } = u.bbox;
      if (x1 < left || x0 > left + width) return null;
      // At least a pixel wide, so that short allocations stay visible.
      return [x0, y0, Math.max(x1 - x0, devicePixelRatio), y1 - y0];
    },

    /** Draws the allocations, or the occupancy, the direction of each channel in its band. */
    drawGrid(u) {
      const res = this.res;
      if (!res || !this.cell) return;
      const { ctx, bbox } = u;
      const colors = {};
      const color = (channel) => (colors[channel] ??= channelColor(channel));
      const borders = {};
      // Allocations are filled with their channel color, as in the legend, and outlined darker.
      const border = (channel) => (borders[channel] ??= darken(color(channel), 0.35));
      ctx.save();
      ctx.beginPath();
      ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
      ctx.clip();
      if (this.fdd) {
        // The gap between the DL and UL grids.
        ctx.fillStyle = cssVar("--grid");
        const [y0, y1] = [u.valToPos(this.band("DL")[0], "y", true), u.valToPos(this.band("UL")[1], "y", true)];
        ctx.fillRect(bbox.left, y0, bbox.width, y1 - y0);
      }
      if (res.mode === "rects") {
        for (const r of res.rects) {
          const [t0, t1] = this.symbolSpan(r.t, r.sym_start, r.sym_length);
          const b = this.box(u, t0, t1, r.dir, r.crb_start, r.crb_length);
          if (!b) continue;
          ctx.globalAlpha = 1;
          ctx.fillStyle = color(r.channel);
          ctx.fillRect(...b);
          if (b[2] > 4 * devicePixelRatio) {
            ctx.globalAlpha = 1;
            ctx.strokeStyle = border(r.channel);
            ctx.lineWidth = devicePixelRatio;
            ctx.strokeRect(...b);
          }
        }
        const members = this.outline?.source === this.panel.source ? new Set(this.outline.members) : null;
        if (members?.size) {
          ctx.globalAlpha = 1;
          ctx.strokeStyle = cssVar("--focus");
          ctx.lineWidth = 2 * devicePixelRatio;
          for (const r of res.rects) {
            if (!r.item || !members.has(`${r.record}:${r.item[0]}:${r.item[1]}`)) continue;
            const [t0, t1] = this.symbolSpan(r.t, r.sym_start, r.sym_length);
            const b = this.box(u, t0, t1, r.dir, r.crb_start, r.crb_length);
            // Outside the allocation, so that its own outline stays visible.
            if (b) ctx.strokeRect(b[0] - devicePixelRatio, b[1] - devicePixelRatio, b[2] + 2 * devicePixelRatio, b[3] + 2 * devicePixelRatio);
          }
        }
      } else if (res.mode === "cells") {
        // One color per cell, of its dominant channel, shaded by the occupancy of all its channels, since in TDD the DL
        // and UL channels of a bin share its cells.
        const g = this.cell.group_size;
        for (const cell of this.mergedCells()) {
          const b = this.box(u, cell.t0 + this.shift, cell.t1 + this.shift, cell.dir, cell.grp * g, g);
          if (!b) continue;
          ctx.globalAlpha = Math.min(1, 0.15 + cell.total);
          ctx.fillStyle = color(cell.channel);
          ctx.fillRect(...b);
        }
      }
      ctx.restore();
    },

    /** The occupancy cells of the response merged per bin, CRB group and band: their dominant channel and total
     * occupancy, cached per response. */
    mergedCells() {
      if (this.merged?.res === this.res) return this.merged.cells;
      const byKey = new Map();
      for (const [t0, t1, grp, channel, occ] of this.res.cells) {
        const dir = this.fdd && UL_CHANNELS.has(channel) ? "UL" : "DL";
        const key = `${t0}|${grp}|${dir}`;
        let cell = byKey.get(key);
        if (!cell) byKey.set(key, (cell = { t0, t1, grp, dir, total: 0, channel, best: -1 }));
        cell.total += occ;
        if (occ > cell.best) Object.assign(cell, { channel, best: occ });
      }
      this.merged = { res: this.res, cells: [...byKey.values()] };
      return this.merged.cells;
    },

    /** The allocation or occupancy cell under a cursor position in CSS pixels, or null. */
    itemAt(u, left, top) {
      const res = this.res;
      if (!res) return null;
      const t = u.posToVal(left, "x");
      const y = u.posToVal(top, "y");
      const inBand = (direction, crb, count) => {
        const [lo] = this.band(direction);
        return y >= lo + crb && y < lo + crb + count;
      };
      if (res.mode === "rects") {
        return res.rects.findLast((r) => {
          const [t0, t1] = this.symbolSpan(r.t, r.sym_start, r.sym_length);
          return t >= t0 && t < t1 && inBand(r.dir, r.crb_start, r.crb_length);
        }) ?? null;
      }
      const g = this.cell.group_size;
      const hits = res.cells.filter(([t0, t1, grp, channel]) => t >= t0 + this.shift && t < t1 + this.shift && inBand(UL_CHANNELS.has(channel) ? "UL" : "DL", grp * g, g));
      return hits.length ? { cells: hits } : null;
    },

    describe(item) {
      if (item.cells) {
        const g = this.cell.group_size;
        const crb = item.cells[0][2] * g;
        return `CRBs ${crb}-${crb + g - 1}: ` + item.cells.map(([, , , ch, occ]) => `${ch} ${Math.round(occ * 100)}%`).join(", ");
      }
      const parts = [
        item.channel,
        item.rnti != null ? `rnti=${hexRnti(item.rnti)}` : null,
        `CRB ${item.crb_start}+${item.crb_length}`,
        `sym ${item.sym_start}+${item.sym_length}`,
      ];
      for (const key of ["mcs", "tbs", "harq_id", "ndi", "rv", "nof_retxs", "k1", "k2", "format", "harq_bits", "sr_bits", "csi_bits", "ssb_index", "row", "grants", "paging_ues",
        "coreset_id", "ss_id", "cce", "nof_cces", "occasion", "fd_ra"]) {
        if (item[key] != null) parts.push(`${key}=${item[key]}`);
      }
      return parts.filter(Boolean).join(" ");
    },

    updateCursor(u) {
      const { left, top } = u.cursor;
      if (left == null || left < 0) {
        this.cursorText = "";
        this.cursorItem = "";
        return;
      }
      const t = u.posToVal(left, "x");
      const slot = this.cell ? this.slotLabel(t) : "";
      this.cursorText = (slot ? `${slot} · ` : "") + formatTime(t, this.timeMode);
      const item = this.hovering && top != null && top >= 0 ? this.itemAt(u, left, top) : null;
      this.cursorItem = item ? this.describe(item) : "";
    },

    /** A click on an allocation opens its slot event; on the occupancy, zooms to ZOOM_SLOTS slots around it. */
    onClick(chart) {
      const { left, top } = chart.cursor;
      if (left == null || left < 0 || top == null || top < 0) return;
      const item = this.itemAt(chart, left, top);
      if (item && !item.cells) {
        this.$emit("select-record", { source: this.panel.source, record: item.record, item: item.item });
        return;
      }
      if (this.mode === "cells") {
        const t = chart.posToVal(left, "x");
        const half = (ZOOM_SLOTS / 2) * this.cell.slot_duration_s;
        this.$emit("zoom", { min: t - half, max: t + half });
      }
    },

    /**
     * Opens the menu of a UE allocation of a HARQ process: a PDSCH, a PUSCH, or the PDCCH of a UE. Elsewhere, and with
     * Shift, the browser's own menu opens.
     */
    onContextMenu(e, chart) {
      this.menu = null;
      if (e.shiftKey) return;
      const rect = chart.over.getBoundingClientRect();
      const item = this.itemAt(chart, e.clientX - rect.left, e.clientY - rect.top);
      if (!item || item.cells || !isHarqAllocation(item)) return;
      e.preventDefault();
      this.menu = { x: e.clientX, y: e.clientY, item, rnti: hexRnti(item.rnti) };
    },

    inspectHarq() {
      const { item } = this.menu;
      this.$emit("inspect-harq", { source: this.panel.source, record: item.record, item: item.item });
      this.menu = null;
    },
  },
  template: `
    <section class="panel grid-panel">
      <header class="panel-bar">
        <strong>Resource grid</strong>
        <select v-if="gridChoices.length > 1" v-model.number="panel.source" :title="source ? source.path : 'Source'">
          <option v-for="s in gridChoices" :key="s.id" :value="s.id">{{ s.file ?? s.name }}</option>
        </select>
        <span v-else-if="source" class="muted">{{ source.file }}</span>
        <span v-if="cell" class="muted">{{ cell.duplex }}, {{ cell.nof_crbs }} CRBs</span>
        <span class="grid-legend">
          <span v-for="c in legend" :key="c.name"><span class="swatch" :style="{ background: c.color }"></span>{{ c.name }}</span>
        </span>
        <span class="status">
          <span v-if="source && (source.status === 'parsing' || source.status === 'deferred')" class="muted">parsing {{ Math.floor((source.progress ?? 0) * 100) }}%</span>
          <span v-else-if="loading" class="muted">loading</span>
          <span v-else class="muted">{{ status }}</span>
          <span v-if="error" class="error">{{ error }}</span>
        </span>
<button class="icon" :title="maximized ? 'Restore (Esc)' : 'Maximize, alone with the log pane'" @click="$emit('maximize')">{{ maximized ? "🗗" : "⛶" }}</button>
        <button class="icon" title="Remove the grid" @click="$emit('remove')">✕</button>
      </header>
      <div class="chart-wrap">
        <div ref="chart" class="chart" :style="{ height: chartHeight + 'px' }"></div>
        <div v-if="cursorText" class="cursor-readout">
          {{ cursorText }}
          <div v-if="cursorItem">{{ cursorItem }}</div>
        </div>
      </div>
      <div v-if="menu" ref="menu" class="context-menu" role="menu" :style="{ left: menu.x + 'px', top: menu.y + 'px' }">
        <button role="menuitem" title="Show the PDCCHs, PDSCHs or PUSCHs, UCI and HARQ-ACK reports of this transport block, from its new transmission to its outcome"
                @click="inspectHarq">Inspect HARQ process {{ menu.item.harq_id }} of {{ menu.rnti }}</button>
      </div>
    </section>
  `,
};
