// SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
// SPDX-License-Identifier: BSD-3-Clause-Open-MPI

// Bumped when the encoded state changes incompatibly.
const VERSION = 3;
const PREFIX = "#v=";
// Plot properties saved in the view state, besides the file of its source.
const PLOT_KEYS = ["kind", "dataset", "instance", "field", "second", "field2", "splitBy", "splitValues", "filter", "mode", "columnFilter", "joined", "groupBy"];

/** @param {string} text */
function toBase64Url(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** @param {string} encoded */
function fromBase64Url(encoded) {
  const binary = atob(encoded.replace(/-/g, "+").replace(/_/g, "/"));
  return new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)));
}

/**
 * Encodes the view as a URL fragment: the open tabs with their plots and zoom, and the selected tab.
 * Runs are identified by name and sources by file name, not path.
 * @param {{runs: Array<{id: number, name: string}>, sources: Array<{file: string}>, tabs: Record<number, any>, activeId: number | null, timeMode: string}} app
 */
export function encodeView(app) {
  const state = {
    v: VERSION,
    timeMode: app.timeMode,
    active: app.runs.find((r) => r.id === app.activeId)?.name ?? null,
    tabs: app.runs
      .filter((r) => app.tabs[r.id])
      .map((r) => ({
        run: r.name,
        range: app.tabs[r.id].userRange,
        events: app.tabs[r.id].eventCategories,
        alone: app.tabs[r.id].relatedAnswered || undefined,
        pane: paneState(app.tabs[r.id].pane, app.sources),
        // Marks are saved by the file of their log and their regex, and marked again when restored.
        marks: (app.tabs[r.id].marks ?? []).map((m) => ({ log: app.sources[m.log]?.file ?? null, pattern: m.pattern, shown: m.shown })),
        plots: app.tabs[r.id].plots.map((p) => ({
          source: app.sources[p.source]?.file ?? null,
          // An ad-hoc metric is saved as its spec, and opened again from its log, the source of the same file.
          adhoc: app.sources[p.source]?.adhoc ?? undefined,
          ...Object.fromEntries(PLOT_KEYS.map((k) => [k, p[k]])),
        })),
      })),
  };
  return PREFIX + toBase64Url(JSON.stringify(state));
}

/**
 * The kept tabs of a log pane, by the file of their source, and the index of the shown one among them. A record of an
 * ad-hoc metric is saved as the line of its log, which has the same number.
 */
function paneState(pane, sources) {
  const kept = (pane?.tabs ?? []).filter((t) => !t.preview);
  if (!kept.length) return undefined;
  const fileOf = (id) => sources[sources[id]?.parent ?? id]?.file ?? null;
  return {
    tabs: kept.map((t) => ({ kind: t.kind, source: fileOf(t.sel.source), record: t.sel.record, item: t.sel.item ?? undefined })),
    active: kept.findIndex((t) => t.id === pane.active),
  };
}

/**
 * Decodes a URL fragment against the open runs, matching tabs to runs by name and plots to sources by file name.
 * Returns null without a view state, or the restored view and warnings about what could not be restored.
 * @param {string} hash
 * @param {Array<{id: number, name: string, sources: number[]}>} runs Open runs.
 * @param {Array<{id: number, file: string, datasets: Array<{name: string}>}>} sources Sources by id.
 */
export function decodeView(hash, runs, sources) {
  if (!hash.startsWith(PREFIX)) return null;
  let state;
  try {
    state = JSON.parse(fromBase64Url(hash.slice(PREFIX.length)));
  } catch {
    return { view: null, warnings: ["The view in the URL is not valid and was ignored."] };
  }
  if (state.v !== VERSION) return { view: null, warnings: ["The view in the URL is from another version and was ignored."] };

  const warnings = [];
  const byName = new Map(runs.map((r) => [r.name, r]));
  const tabs = [];
  for (const t of state.tabs ?? []) {
    const run = byName.get(t.run);
    if (!run) {
      warnings.push(`${t.run} of the URL view is not open, its tab was skipped. Open it with the + tab.`);
      continue;
    }
    const byFile = new Map(run.sources.map((id) => [sources[id]?.file, sources[id]]));
    const plots = [];
    for (const p of t.plots ?? []) {
      const source = byFile.get(p.source);
      if (!source) {
        warnings.push(`${p.source} is not in ${t.run}, its plot was skipped.`);
        continue;
      }
      // Datasets are only known for parsed sources, so plots of sources still parsing are kept as they are. Plots
      // without a dataset, e.g. of a tab never shown, get one when shown.
      if (!p.adhoc && p.dataset != null && source.datasets.length && !source.datasets.some((d) => d.name === p.dataset)) {
        warnings.push(`Dataset ${p.dataset} is not in ${p.source}, its plot was skipped.`);
        continue;
      }
      plots.push({
        ...p,
        source: source.id,
        kind: p.kind ?? "plot",
        splitValues: p.splitValues ?? [],
        filter: p.filter ?? "",
        columnFilter: p.columnFilter ?? "",
      });
    }
    const marks = [];
    for (const m of t.marks ?? []) {
      const log = byFile.get(m.log);
      if (log) marks.push({ source: log.id, pattern: m.pattern, shown: m.shown !== false });
      else warnings.push(`${m.log} is not in ${t.run}, its mark /${m.pattern}/ was skipped.`);
    }
    const pane = [];
    for (const [i, r] of (t.pane?.tabs ?? []).entries()) {
      const source = byFile.get(r.source);
      if (source) pane.push({ kind: r.kind ?? "record", source: source.id, record: r.record, item: r.item ?? null, active: i === t.pane.active });
      else warnings.push(`${r.source} is not in ${t.run}, its tab of the log pane was skipped.`);
    }
    tabs.push({ run: run.id, range: t.range ?? null, plots, marks, pane, eventCategories: t.events ?? null, relatedAnswered: Boolean(t.alone) });
  }
  const active = byName.get(state.active)?.id ?? null;
  return { view: { timeMode: state.timeMode, active, tabs }, warnings };
}
