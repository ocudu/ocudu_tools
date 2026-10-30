// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

// Tables of the decoded event of a scheduler event trace: the decisions of a slot, one table per kind of allocation,
// and its events, the scheduler inputs, one table per kind of input.

// Lists of the decision of a slot, in display order, with their titles.
const DECISIONS = [
  ["dl_pdcchs", "DL PDCCH"], ["ul_pdcchs", "UL PDCCH"], ["ue_pdschs", "UE PDSCH"], ["puschs", "PUSCH"], ["pucchs", "PUCCH"],
  ["sib_pdschs", "SIB PDSCH"], ["rar_pdschs", "RAR PDSCH"], ["paging_pdschs", "Paging PDSCH"], ["ssbs", "SSB"],
  ["csi_rs", "CSI-RS"], ["prachs", "PRACH"], ["srss", "SRS"], ["failed_attempts", "Failed attempts"],
];
// Titles of the inputs of a slot, by type, in display order.
const INPUTS = [["HarqAckEvent", "HARQ-ACK"], ["CrcEvent", "UL CRC"], ["SrEvent", "SR"], ["CsiReportEvent", "CSI report"], ["RachIndication", "RACH"]];
// Items of a list beyond which a nested list is shown by its length rather than as columns.
const MAX_NESTED_ITEMS = 4;
// Fields that hold a slot count, shown in SFN.slot.
const SLOT_FIELDS = new Set(["slot_tx", "slot_rx", "anchor_slot"]);

/** Whether a field holds an RNTI, e.g. "rnti" or "tc_rnti", shown in hex. @param {string} key */
function isRnti(key) {
  return key.endsWith("rnti");
}

/**
 * A row of a table from an item: nested objects and short lists of objects as dotted columns, e.g.
 * "rbs.vrb_start" or "codewords.0.mcs_index", lists of values joined, longer lists of objects by their length.
 * @param {Record<string, any>} item
 * @param {string} [prefix]
 * @param {Record<string, string>} [row]
 */
function flatten(item, slotsPerFrame, prefix = "", row = {}) {
  for (const [key, value] of Object.entries(item)) {
    const name = prefix + key;
    if (value == null) {
      row[name] = "";
    } else if (Array.isArray(value)) {
      if (value.every((v) => v == null || typeof v !== "object")) row[name] = value.join(",");
      else if (value.length > MAX_NESTED_ITEMS) row[name] = `${value.length} items`;
      else value.forEach((v, i) => flatten(v ?? {}, slotsPerFrame, `${name}.${i}.`, row));
    } else if (typeof value === "object") {
      flatten(value, slotsPerFrame, `${name}.`, row);
    } else if (typeof value === "number" && SLOT_FIELDS.has(key) && slotsPerFrame) {
      row[name] = sfnSlot(value, slotsPerFrame);
    } else if (typeof value === "number" && isRnti(name)) {
      row[name] = `0x${value.toString(16).padStart(4, "0")}`;
    } else {
      row[name] = String(value);
    }
  }
  return row;
}

/** SFN.slot of a slot count. @param {number} count @param {number} slotsPerFrame */
function sfnSlot(count, slotsPerFrame) {
  return `${Math.floor(count / slotsPerFrame)}.${count % slotsPerFrame}`;
}

/**
 * A table of items: their columns, in the order first seen, and their rows.
 * @param {Array<Record<string, any>>} items
 * @param {number | null} slotsPerFrame
 */
function table(items, slotsPerFrame) {
  const rows = items.map((item) => flatten(item, slotsPerFrame));
  const columns = [];
  for (const row of rows) for (const key of Object.keys(row)) if (!columns.includes(key)) columns.push(key);
  return { columns, rows };
}

/** The RACH indications of a slot as one row per detected preamble, with its indication and occasion. */
function rachRows(indications) {
  const rows = [];
  for (const ind of indications) {
    for (const occ of ind.occasions ?? []) {
      for (const p of occ.preambles ?? []) {
        rows.push({ slot_rx: ind.slot_rx, start_symbol: occ.start_symbol, freq_idx: occ.freq_idx, ...p });
      }
    }
  }
  return rows;
}

export default {
  name: "EventTables",
  props: {
    // Event of schedtrace.read().
    event: { type: Object, required: true },
    // "decisions" or "events".
    view: { type: String, required: true },
    // Decision item to highlight, as [list, index], e.g. ["ue_pdschs", 3], or null.
    highlight: { type: Array, default: null },
    // Slots per frame of the cell, to show slot counts in SFN.slot, or null to show them as counts.
    slotsPerFrame: { type: Number, default: null },
  },
  watch: {
    highlight() {
      this.scrollToHighlight();
    },
    event() {
      this.scrollToHighlight();
    },
    view() {
      this.scrollToHighlight();
    },
  },
  mounted() {
    this.scrollToHighlight();
  },
  methods: {
    isHighlighted(t, i) {
      return this.view === "decisions" && this.highlight != null && this.highlight[0] === t.key && this.highlight[1] === i;
    },

    /** Scrolls the pane, not the page, to the highlighted row. */
    async scrollToHighlight() {
      await this.$nextTick();
      const row = this.$el?.querySelector?.(".event-table tr.selected");
      const pane = this.$el?.closest?.(".record-detail");
      if (!row || !pane) return;
      const top = row.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
      pane.scrollTop = Math.max(0, top - pane.clientHeight / 2);
    },
  },
  computed: {
    slotLabel() {
      return this.slotsPerFrame ? sfnSlot(this.event.slot_tx, this.slotsPerFrame) : String(this.event.slot_tx);
    },
    /** The tables of the view, each with its title and count of items. */
    tables() {
      const ev = this.event;
      if (ev.type !== "CellSlotEvent") {
        const { type, ...fields } = ev;
        return [{ title: type ?? "event", count: 1, ...table([fields], this.slotsPerFrame) }];
      }
      if (this.view === "decisions") {
        const decision = ev.decision ?? {};
        return DECISIONS.filter(([key]) => decision[key] != null && (!Array.isArray(decision[key]) || decision[key].length))
          .map(([key, title]) => {
            const items = Array.isArray(decision[key]) ? decision[key] : [decision[key]];
            return { key, title, count: items.length, ...table(items, this.slotsPerFrame) };
          });
      }
      const inputs = (ev.inputs ?? []).filter(Boolean);
      return INPUTS.map(([type, title]) => {
        const items = inputs.filter((i) => i.type === type).map(({ type: _, ...rest }) => rest);
        const rows = type === "RachIndication" ? rachRows(items) : items;
        return { key: type, title, count: rows.length, ...table(rows, this.slotsPerFrame) };
      }).filter((t) => t.count);
    },
  },
  template: `
    <div class="event-tables">
      <p class="muted">
        <template v-if="event.type === 'CellSlotEvent'">slot={{ slotLabel }} latency={{ event.latency_us }} us</template>
        <template v-else>{{ event.type }}</template>
      </p>
      <p v-if="!tables.length" class="muted">no {{ view }} in this slot</p>
      <section v-for="t in tables" :key="t.title">
        <h4>{{ t.title }} <span class="muted">({{ t.count }})</span></h4>
        <div class="event-table-wrap">
          <table class="event-table">
            <thead><tr><th v-for="c in t.columns" :key="c">{{ c }}</th></tr></thead>
            <tbody>
              <tr v-for="(row, i) in t.rows" :key="i" :class="{ selected: isHighlighted(t, i) }"><td v-for="c in t.columns" :key="c">{{ row[c] }}</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  `,
};
