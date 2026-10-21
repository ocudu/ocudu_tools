// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

import { getJSON } from "../api.js";

/** The HARQ transaction of an allocation of a scheduler event trace, in a tab of the log pane: its PDUs and reports. */
export default {
  name: "HarqView",
  props: {
    // The allocation, as {source, record, item}.
    selection: { type: Object, required: true },
    // Whether its tab is the shown one.
    active: { type: Boolean, default: true },
    // Height of the table, in pixels, or null for the default.
    height: { type: Number, default: null },
    maximized: { type: Boolean, default: false },
  },
  emits: ["focus", "interact", "title", "loaded", "select-record"],
  data() {
    // current is the index of the highlighted row.
    return { tr: null, error: "", current: null };
  },
  computed: {
    title() {
      const tr = this.tr;
      return tr ? `HARQ ${tr.dir} ${tr.rnti} h${tr.harq_id} · ${tr.rows[0]?.slot ?? ""}` : null;
    },
    tableStyle() {
      return this.height && !this.maximized ? { height: this.height + "px" } : null;
    },
  },
  watch: {
    selection: {
      handler() {
        this.load();
      },
      immediate: true,
    },
    active(active) {
      if (active) this.emitFocus();
    },
    title(title) {
      this.$emit("title", title);
    },
  },
  methods: {
    async load() {
      const { source, record, item } = this.selection;
      try {
        const tr = await getJSON("/api/harq", { source, record, item: item.join(":") });
        this.tr = tr;
        this.error = "";
        const at = tr.rows.findIndex((r) => r.record === record && r.item?.[0] === item[0] && r.item?.[1] === item[1]);
        this.current = at >= 0 ? at : 0;
        this.$emit("loaded", {
          key: `${tr.dir}|${tr.rnti}|${tr.harq_id}|${tr.rows[0]?.record}`,
          members: tr.rows.filter((r) => r.item).map((r) => `${r.record}:${r.item[0]}:${r.item[1]}`),
        });
        this.emitFocus();
      } catch (e) {
        this.tr = null;
        this.error = e.message;
      }
    },

    /** Focuses the slot of the highlighted row on the plots and the grid. */
    emitFocus() {
      const row = this.tr?.rows[this.current];
      if (row) this.$emit("focus", { source: this.selection.source, record: row.record, t: row.t });
    },

    select(index) {
      this.current = index;
      this.$emit("interact");
      this.emitFocus();
    },

    /** Fields of a row as key=value text. */
    params(row) {
      return Object.entries(row.params)
        .filter(([, v]) => v != null)
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");
    },

    /** Time of a row after the first one, in ms. */
    offset(row) {
      return ((row.t - this.tr.rows[0].t) * 1e3).toFixed(1);
    },
  },
  template: `
    <section class="record-view harq-view">
      <header class="panel-bar">
        <template v-if="tr">
          <strong>HARQ {{ tr.dir }} {{ tr.rnti }} h{{ tr.harq_id }}</strong>
          <span v-if="tr.ue_index != null" class="muted">ue={{ tr.ue_index }}</span>
          <span v-if="tr.tbs != null" class="muted">TBS {{ tr.tbs }} B</span>
          <span :class="tr.ended_by === 'ack' ? '' : 'harq-failed'">{{ tr.outcome }}</span>
          <span v-if="!tr.started" class="muted">new transmission not found within the 1024 slots before</span>
        </template>
        <span v-else-if="!error" class="muted">loading</span>
        <span class="status"><span v-if="error" class="error">{{ error }}</span></span>
      </header>
      <div v-if="tr" class="harq-table table-scroll" :style="tableStyle">
        <table>
          <thead>
            <tr><th>+ms</th><th>SFN.slot</th><th>kind</th><th>tx</th><th>rnti</th><th>parameters</th></tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in tr.rows" :key="i" :class="['clickable', { selected: i === current }]"
                title="Click to focus its slot, double-click to open its slot event" @click="select(i)"
                @dblclick="$emit('select-record', { source: selection.source, record: r.record, item: r.item })">
              <td>{{ offset(r) }}</td><td>{{ r.slot }}</td><td>{{ r.kind }}</td><td>{{ r.tx }}</td><td>{{ tr.rnti }}</td>
              <td class="harq-params">{{ params(r) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  `,
};
