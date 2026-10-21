// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import { getJSON } from "../api.js";
import EventTables from "./event-tables.js";

// Records first shown around the selected one.
const WINDOW = 61;
// Records loaded when the lines are scrolled near their first or last one, and records kept at most, those farthest
// from the scrolled end dropped, so that the page holds a bounded part of the file.
const PAGE = 200;
const MAX_RECORDS = 1000;
// Time at the start of a record: whole seconds, then their fraction, UTC.
const RECORD_TIME_RE = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?/;
// Characters of a selection that its lines can be marked by.
const MAX_MARK_TEXT = 200;
// Distance from the first or last line, in pixels, at which the next records are loaded.
const LOAD_MARGIN = 300;
// Slot of the summary line of a scheduler trace event, e.g. slot=1023.7.
const SLOT_RE = /\bslot=(\d+\.\d+)/;
/** Records of a source around a selected one, in a tab of the log pane, with their decoding for sources that have one. */
export default {
  name: "RecordView",
  props: {
    selection: { type: Object, required: true },
    sources: { type: Array, required: true },
    // Whether its tab is the shown one.
    active: { type: Boolean, default: true },
    // Height of the lines, in pixels, or null for the default.
    height: { type: Number, default: null },
    // Whether the pane fills the window.
    maximized: { type: Boolean, default: false },
  },
  components: { EventTables },
  emits: ["select-record", "plot-key", "mark-lines", "focus", "interact", "title"],
  data() {
    // current is the highlighted record, the selected one until another record is clicked, and detail its decoding.
    return {
      records: [], center: 0, error: "", current: null, detail: "", detailError: "",
      // Whether the first and the last record of the file are loaded, and whether more records are being loaded.
      atStart: false, atEnd: false, paging: false,
      // Count of the loads of the pane, which tells pages of an earlier load apart.
      generation: 0,
      // Decoded record as a structure, for sources that give one, and the view of it: "decisions", "events" or "raw".
      detailData: null, detailTab: "decisions",
      // Search of the records: its text, whether it is a regular expression, and its outcome, e.g. "no match".
      query: "", regex: false, searchNote: "", searching: false,
      // Context menu of a line, at its position in the window, with the key it plots and the text it marks, if any.
      menu: null,
    };
  },
  computed: {
    source() {
      return this.sources[this.selection.source];
    },
    sourceName() {
      return this.source?.name ?? "";
    },
    /** Log whose lines ad-hoc metrics are picked from: the source, or the log of an ad-hoc metric. */
    logSource() {
      if (this.source?.parent != null) return this.sources[this.source.parent];
      return this.source?.type === "log_metrics" ? this.source : null;
    },
    /** Matcher of the search in the shown lines, to highlight its matches, or null. JavaScript regular expressions
     * mostly agree with the Python ones of the server; a pattern only the server takes is not highlighted. */
    hitPattern() {
      if (!this.query) return null;
      try {
        return new RegExp(this.regex ? this.query : this.query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
      } catch {
        return null;
      }
    },
    /** Whether records decode, e.g. pcap frames, which are then shown beside the records. */
    hasDetail() {
      return Boolean(this.source?.has_detail);
    },
    /** Short name of the highlighted record, for the label of its tab: the slot of a trace event, else its number. */
    title() {
      const r = this.records.find((r) => r.record === this.current);
      const slot = r && SLOT_RE.exec(r.text);
      return slot ? slot[1] : this.current == null ? null : `#${this.current}`;
    },
    linesStyle() {
      return this.height && !this.maximized ? { height: this.height + "px" } : null;
    },
  },
  watch: {
    /** The focus of the tab is that of the pane when the tab is shown. */
    async active(active) {
      const lines = this.$refs.lines;
      // A hidden tab has no layout, which loses the scroll position of its lines.
      if (!active) {
        this.scrolled = lines ? [lines.scrollTop, lines.scrollLeft] : null;
        return;
      }
      this.emitFocus();
      await this.$nextTick();
      if (lines && this.scrolled) [lines.scrollTop, lines.scrollLeft] = this.scrolled;
    },
    async maximized() {
      await this.$nextTick();
      this.centerCurrent();
    },
    title: {
      handler(title) {
        this.$emit("title", title);
      },
      immediate: true,
    },
    selection: {
      handler() {
        this.current = this.selection.record;
        // An allocation picked in the resource grid shows in the decisions of its slot.
        if (this.selection.item) this.detailTab = "decisions";
        this.load(this.selection.record);
        this.loadDetail();
      },
      immediate: true,
      deep: true,
    },
  },
  created() {
    // Not reactive: scroll position of the lines while the tab is hidden.
    this.scrolled = null;
  },
  mounted() {
    // In the capture phase, so that it runs before the app's own Esc handling.
    this.keyListener = (e) => this.onKey(e);
    window.addEventListener("keydown", this.keyListener, true);
    // A press anywhere but on the menu closes it.
    this.pointerListener = (e) => {
      if (this.menu && !this.$refs.menu?.contains(e.target)) this.menu = null;
    };
    window.addEventListener("pointerdown", this.pointerListener, true);
  },
  beforeUnmount() {
    window.removeEventListener("keydown", this.keyListener, true);
    window.removeEventListener("pointerdown", this.pointerListener, true);
  },
  methods: {
    /** Highlights a record, focuses its time on the plots, and shows its decoding, for sources whose records decode. */
    select(record) {
      // Selecting text in a line is not a click on it.
      if (!window.getSelection()?.isCollapsed) return;
      this.current = record;
      this.emitFocus();
      if (this.hasDetail) this.loadDetail();
    },

    /** Focuses the highlighted record on the plots, at the time of its line or of the entry it continues. */
    emitFocus() {
      const index = this.records.findIndex((r) => r.record === this.current);
      for (let i = index; i >= 0; i--) {
        const m = RECORD_TIME_RE.exec(this.records[i].text);
        if (!m) continue;
        const t = Date.parse(m[1] + "Z") / 1000 + Number(m[2] ?? 0);
        this.$emit("focus", { source: this.selection.source, record: this.current, t });
        return;
      }
    },

    async loadDetail() {
      this.detail = "";
      this.detailData = null;
      this.detailError = "";
      if (!this.hasDetail) return;
      const record = this.current;
      try {
        const res = await getJSON("/api/records/detail", { source: this.selection.source, record });
        if (record === this.current) {
          this.detail = res.text;
          this.detailData = res.data ?? null;
        }
      } catch (e) {
        this.detailError = e.message;
      }
    },

    /**
     * Splits the text of a record at its marks, which are [start, end) spans.
     * @returns {{text: string, mark: boolean}[]}
     */
    segments(r) {
      const parts = [];
      let pos = 0;
      for (const [start, end] of this.spans(r)) {
        if (start > pos) parts.push({ text: r.text.slice(pos, start), mark: false });
        parts.push({ text: r.text.slice(start, end), mark: true });
        pos = end;
      }
      if (pos < r.text.length || !parts.length) parts.push({ text: r.text.slice(pos), mark: false });
      return parts;
    },

    /** Marks of a record: the spans of its field, and the matches of the search, sorted and without overlaps. */
    spans(r) {
      const spans = [...(r.marks ?? [])];
      const hit = this.hitPattern;
      if (hit) {
        for (const m of r.text.matchAll(hit)) {
          if (m[0].length) spans.push([m.index, m.index + m[0].length]);
        }
      }
      spans.sort((a, b) => a[0] - b[0]);
      const out = [];
      for (const s of spans) {
        if (out.length && s[0] < out[out.length - 1][1]) out[out.length - 1][1] = Math.max(out[out.length - 1][1], s[1]);
        else out.push([...s]);
      }
      return out;
    },

    /** Goes to the next record matching the search after the highlighted one, or the previous one with backward. */
    async search(backward = false) {
      if (!this.query || this.searching) return;
      this.$emit("interact");
      this.searching = true;
      try {
        const res = await getJSON("/api/records/search", {
          source: this.selection.source, q: this.query, regex: this.regex, after: this.current ?? this.center, backward,
        });
        this.searchNote = res.record == null ? "no match" : res.wrapped ? (backward ? "wrapped to the end" : "wrapped to the start") : "";
        if (res.record != null) {
          this.current = res.record;
          await this.load(res.record);
          this.loadDetail();
        }
      } catch (e) {
        this.searchNote = e.message;
      } finally {
        this.searching = false;
      }
    },

    /**
     * Opens the menu of a line: to plot the numeric key=value field right-clicked, or the key selected, e.g. "tbs" of
     * "tbs=5889", and to mark the lines with the text selected. Elsewhere, and with Shift, the browser's own menu opens.
     */
    onContextMenu(e) {
      this.menu = null;
      if (e.shiftKey || !this.logSource) return;
      const text = this.selectedText();
      const key = (text && this.keyOf(text)) ?? this.keyAt(e.clientX, e.clientY);
      if (!key && !text) return;
      e.preventDefault();
      this.$emit("interact");
      this.menu = { x: e.clientX, y: e.clientY, key, text };
    },

    /** The text selected in the lines, of at most MAX_MARK_TEXT characters, or null. */
    selectedText() {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || !this.$refs.lines.contains(selection.anchorNode)) return null;
      const text = selection.toString().trim();
      return text && text.length <= MAX_MARK_TEXT && !text.includes("\n") ? text : null;
    },

    /** The key of a text that is a key or a key=value field, or null. */
    keyOf(text) {
      const m = /^([\w.\-\[\]]+?)(=\S*)?$/.exec(text);
      return m ? m[1] : null;
    },

    /** The key of the key=value field with a numeric value at a point of the window, or null. */
    keyAt(x, y) {
      const caret = document.caretPositionFromPoint?.(x, y);
      const range = caret ? null : document.caretRangeFromPoint?.(x, y);
      const node = caret?.offsetNode ?? range?.startContainer;
      const offset = caret?.offset ?? range?.startOffset;
      const text = node?.parentElement?.closest(".text");
      if (!text || !this.$refs.lines.contains(text)) return null;
      // Matches split the text of a line into several nodes, so the offset is counted from the start of the line.
      let at = 0;
      const walker = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n && n !== node; n = walker.nextNode()) at += n.textContent.length;
      at += offset;
      // Numbers as the ad-hoc scan reads them, with an optional unit, e.g. 55.5Mbps, but not 0x44.
      for (const m of text.textContent.matchAll(/(?<![\w.\-\[\]])([\w.\-\[\]]+)=[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?[A-Za-z%]*(?![\w.])/g)) {
        if (m.index <= at && at <= m.index + m[0].length) return m[1];
      }
      return null;
    },

    plotMenuKey() {
      this.$emit("plot-key", { source: this.logSource.id, key: this.menu.key });
      this.menu = null;
    },

    /** Marks the lines with the selected text, matched literally and ignoring case. */
    markMenuText() {
      this.$emit("mark-lines", { source: this.logSource.id, pattern: this.menu.text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") });
      this.menu = null;
    },

    /** Shows the same record among all the lines of the log of an ad-hoc metric. */
    showAllLines() {
      this.$emit("select-record", { source: this.source.parent, record: this.current ?? this.selection.record });
    },

    /** Loads the records before or after the loaded ones when the lines are scrolled near their first or last one. */
    onScroll() {
      this.menu = null;
      const lines = this.$refs.lines;
      if (!lines || this.paging || !this.records.length) return;
      if (lines.scrollTop < LOAD_MARGIN && !this.atStart) this.loadPage(true);
      else if (lines.scrollHeight - lines.scrollTop - lines.clientHeight < LOAD_MARGIN && !this.atEnd) this.loadPage(false);
    },

    /**
     * Loads PAGE records before the first loaded one, or after the last, keeping the lines in view where they are, and
     * drops records from the other end beyond MAX_RECORDS.
     */
    async loadPage(before) {
      this.paging = true;
      const generation = this.generation;
      try {
        const first = this.records[0].record;
        const last = this.records[this.records.length - 1].record;
        // Records are fetched by position, since those of an ad-hoc metric are some lines of the log only.
        const cursor = before ? { before: first } : { after: last };
        const page = await getJSON("/api/records", { source: this.selection.source, count: PAGE, ...cursor });
        if (generation !== this.generation) return;
        const fresh = page.filter((r) => (before ? r.record < first : r.record > last));
        const lines = this.$refs.lines;
        if (before) {
          this.atStart = page.length < PAGE;
          const firstTop = lines.firstElementChild?.offsetTop ?? 0;
          this.records = [...fresh, ...this.records];
          if (this.records.length > MAX_RECORDS) {
            this.records = this.records.slice(0, MAX_RECORDS);
            this.atEnd = false;
          }
          await this.$nextTick();
          // The prepended lines push the ones in view down by their height, the offset of the line that was first.
          const formerFirst = lines.children[fresh.length];
          if (formerFirst) lines.scrollTop += formerFirst.offsetTop - firstTop;
        } else {
          this.atEnd = fresh.length < PAGE;
          this.records = [...this.records, ...fresh];
          const excess = this.records.length - MAX_RECORDS;
          if (excess > 0) {
            // Measured once the appended lines are rendered.
            await this.$nextTick();
            const height = lines.scrollHeight;
            this.records = this.records.slice(excess);
            this.atStart = false;
            await this.$nextTick();
            // The dropped first lines pull the ones in view up by their height.
            lines.scrollTop -= height - lines.scrollHeight;
          }
        }
        this.error = "";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.paging = false;
      }
    },

    /** Closes the menu on Esc, before the pane restores itself and the app restores a maximized panel. */
    onKey(e) {
      if (e.key !== "Escape" || !this.menu) return;
      e.preventDefault();
      e.stopPropagation();
      this.menu = null;
    },

    /** Scrolls the log lines to the highlighted record. */
    centerCurrent() {
      const lines = this.$refs.lines;
      const selected = lines?.querySelector(".record-line.selected");
      if (selected) lines.scrollTop = selected.offsetTop - (lines.clientHeight - selected.offsetHeight) / 2;
    },

    async load(center) {
      this.center = Math.max(1, center);
      // Pages requested before this load are dropped when they arrive.
      const generation = ++this.generation;
      try {
        const { source, record, field } = this.selection;
        const records = await getJSON("/api/records", { source, around: this.center, count: WINDOW, mark: field ? record : null, field });
        if (generation !== this.generation) return;
        this.records = records;
        this.atStart = !records.length || records[0].record === 1;
        // The ends of an ad-hoc metric's records are found by paging, which loads nothing past them.
        this.atEnd = !records.length;
        this.error = "";
      } catch (e) {
        this.error = e.message;
      }
      await this.$nextTick();
      this.emitFocus();
      // Only the log lines scroll: scrollIntoView would also scroll the page, moving the clicked plot away.
      this.centerCurrent();
      const lines = this.$refs.lines;
      const selected = lines?.querySelector(".record-line.selected");
      const mark = selected?.querySelector("mark");
      if (mark) {
        // Marks near the end of long lines would be out of view.
        const left = mark.getBoundingClientRect().left - lines.getBoundingClientRect().left + lines.scrollLeft;
        if (left < lines.scrollLeft || left + mark.offsetWidth > lines.scrollLeft + lines.clientWidth) {
          lines.scrollLeft = Math.max(0, left - lines.clientWidth / 3);
        }
      }
    },
  },
  template: `
    <section class="record-view">
      <header class="panel-bar">
        <strong>{{ sourceName }}</strong>
        <span class="muted">{{ hasDetail ? "frame" : "line" }} {{ current }}</span>
        <form class="filter record-search" @submit.prevent="search(false)">
          <input v-model="query" placeholder="search" title="Enter: next match, Shift+Enter: previous. Case-insensitive. Right-click a key=value field of a line to plot it, or selected text to mark the lines with it."
                 @keydown.enter.shift.prevent="search(true)" @input="searchNote = ''" />
        </form>
        <label class="inline" title="Search with a regular expression (Python syntax), e.g. ue=\d+ .*RLF">
          <input type="checkbox" v-model="regex" @change="searchNote = ''" /> regex
        </label>
        <button class="icon" title="Previous match (Shift+Enter)" :disabled="!query" @click="search(true)">↑</button>
        <button class="icon" title="Next match (Enter)" :disabled="!query" @click="search(false)">↓</button>
        <span v-if="searching" class="muted">searching</span>
        <span v-else-if="searchNote" :class="searchNote === 'no match' || searchNote.startsWith('wrapped') ? 'muted' : 'error'">{{ searchNote }}</span>
        <span v-if="source?.adhoc" class="filtered-chip" :title="'Only the lines with values of ' + source.adhoc.key + '='">filtered</span>
        <button v-if="source?.adhoc" title="Show this line among all the lines of the log" @click="showAllLines">all lines</button>
        <span class="status"><span v-if="error" class="error">{{ error }}</span></span>
      </header>
      <div :class="['record-body', { split: hasDetail }]">
        <div ref="lines" class="record-lines" @scroll.passive="onScroll" @wheel.passive="$emit('interact')" @pointerdown="$emit('interact')" @contextmenu="onContextMenu" :style="linesStyle">
          <div v-for="r in records" :key="r.record"
               :class="['record-line', 'clickable', { selected: r.record === current }]" @click="select(r.record)">
            <span class="lineno">{{ r.record }}</span><span class="text"><template v-for="(part, i) in segments(r)" :key="i"><mark v-if="part.mark">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
          </div>
        </div>
        <div v-if="hasDetail && detailData" class="record-detail record-detail-tabs" :style="linesStyle">
          <div class="detail-tabs">
            <button v-for="tab in ['decisions', 'events', 'raw']" :key="tab" :class="{ active: detailTab === tab }" @click="detailTab = tab; $emit('interact')">{{ tab }}</button>
          </div>
          <pre v-if="detailTab === 'raw'">{{ detail }}</pre>
          <event-tables v-else :event="detailData" :view="detailTab" :slots-per-frame="source?.grid?.slots_per_frame ?? null"
                        :highlight="current === selection.record ? selection.item ?? null : null" />
        </div>
        <pre v-else-if="hasDetail" class="record-detail" :style="linesStyle"><span v-if="detailError" class="error">{{ detailError }}</span>{{ detail || (detailError ? "" : "decoding…") }}</pre>
      </div>
      <div v-if="menu" ref="menu" class="context-menu" role="menu" :style="{ left: menu.x + 'px', top: menu.y + 'px' }">
        <button v-if="menu.key" role="menuitem" title="Plot the values of this key=value field over time, from all the lines of the log"
                @click="plotMenuKey">Plot {{ menu.key }}=</button>
        <button v-if="menu.text" role="menuitem" title="Mark the lines of the log with this text, ignoring case, as events on the plots"
                @click="markMenuText">Mark lines with “{{ menu.text.length > 40 ? menu.text.slice(0, 40) + "…" : menu.text }}”</button>
      </div>
    </section>
  `,
};
