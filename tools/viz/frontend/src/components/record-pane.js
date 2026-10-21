// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import RecordView from "./record-view.js";

// Browser storage key of the height of the pane's content, in pixels.
const HEIGHT_KEY = "ocudu-viz-log-height";
const MIN_HEIGHT = 80;
// Maximum height, and the default one, as fractions of the window height.
const MAX_HEIGHT_FRACTION = 0.85;
const DEFAULT_HEIGHT_FRACTION = 0.35;

/** @returns {number | null} */
function loadHeight() {
  try {
    const h = Number(localStorage.getItem(HEIGHT_KEY));
    return h > 0 ? h : null;
  } catch {
    // Storage can be unavailable, e.g. in private windows.
    return null;
  }
}

/** @param {number | null} height */
function saveHeight(height) {
  try {
    if (height == null) localStorage.removeItem(HEIGHT_KEY);
    else localStorage.setItem(HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // Storage can be unavailable, e.g. in private windows.
  }
}

/** Log pane: the tabs of the records opened from the panels, one shown at a time. */
export default {
  name: "RecordPane",
  props: {
    // Tabs as {id, kind, sel, preview, title}, sel being {source, record, item, field}.
    tabs: { type: Array, required: true },
    active: { type: Number, default: null },
    sources: { type: Array, required: true },
  },
  components: { RecordView },
  emits: ["close", "activate", "close-tab", "pin", "title", "focus", "select-record", "plot-key", "mark-lines"],
  data() {
    return {
      height: loadHeight(),
      // Whether the pane fills the window.
      maximized: false,
    };
  },
  mounted() {
    // In the capture phase, so that it runs before the app's own Esc handling.
    this.keyListener = (e) => this.onKey(e);
    window.addEventListener("keydown", this.keyListener, true);
  },
  beforeUnmount() {
    window.removeEventListener("keydown", this.keyListener, true);
  },
  methods: {
    /** Label of a tab: its source, and its record for sources whose records decode, e.g. a slot of a trace. */
    label(tab) {
      const source = this.sources[tab.sel.source];
      const name = source?.parent != null ? `${this.sources[source.parent]?.file ?? ""} ${source.name}` : source?.file ?? "";
      if (!source?.has_detail) return name;
      return `${name} · ${tab.title ?? "#" + tab.sel.record}`;
    },

    /** Resizes the content by dragging the top edge of the pane. */
    startResize(e) {
      e.preventDefault();
      const handle = e.currentTarget;
      handle.setPointerCapture(e.pointerId);
      const startY = e.clientY;
      const startHeight = this.height ?? window.innerHeight * DEFAULT_HEIGHT_FRACTION;
      const onMove = (ev) => {
        const max = window.innerHeight * MAX_HEIGHT_FRACTION;
        this.height = Math.min(max, Math.max(MIN_HEIGHT, startHeight + startY - ev.clientY));
      };
      const onUp = () => {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        saveHeight(this.height);
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
    },

    resetHeight() {
      this.height = null;
      saveHeight(null);
    },

    /** Restores the maximized pane on Esc, before the app restores a maximized panel behind it. */
    onKey(e) {
      if (e.key !== "Escape" || !this.maximized || e.defaultPrevented) return;
      e.stopPropagation();
      this.maximized = false;
    },

    /** Closes a tab with the middle button, as browsers do. */
    onAuxClick(e, tab) {
      if (e.button === 1) this.$emit("close-tab", tab.id);
    },
  },
  template: `
    <aside :class="['records', { maximized }]">
      <div v-if="!maximized" class="resize-handle" title="Drag to resize, double-click to reset" @pointerdown="startResize" @dblclick="resetHeight"></div>
      <nav class="pane-tabs" role="tablist">
        <span v-for="t in tabs" :key="t.id" role="tab" :aria-selected="t.id === active"
              :class="['pane-tab', { active: t.id === active, preview: t.preview }]"
              :title="t.preview ? 'Preview: the next record opened replaces it. Double-click to keep it.' : ''"
              @click="$emit('activate', t.id)" @dblclick="$emit('pin', t.id)" @auxclick="onAuxClick($event, t)">
          {{ label(t) }}
          <button class="icon" title="Close this tab" @click.stop="$emit('close-tab', t.id)">×</button>
        </span>
        <span class="status"></span>
        <button class="icon" :title="maximized ? 'Restore (Esc)' : 'Maximize'" @click="maximized = !maximized">{{ maximized ? "🗗" : "⛶" }}</button>
        <button class="icon" title="Close all the tabs" @click="$emit('close')">✕</button>
      </nav>
      <template v-for="t in tabs" :key="t.id">
        <record-view v-show="t.id === active" :selection="t.sel" :sources="sources" :active="t.id === active"
                     :height="height" :maximized="maximized"
                     @focus="t.id === active && $emit('focus', $event)" @interact="t.preview && $emit('pin', t.id)"
                     @title="$emit('title', { id: t.id, title: $event })"
                     @select-record="$emit('select-record', $event)" @plot-key="$emit('plot-key', $event)"
                     @mark-lines="$emit('mark-lines', $event)" />
      </template>
    </aside>
  `,
};
