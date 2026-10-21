# amari-ue conventions

Type-specific deltas for `amari-ue` artifacts (Amarisoft UE simulator). The
shared resolve / efficiency / memory flow lives in the skill `SKILL.md`; this
file carries only what is particular to Amarisoft UE logs.

A single run directory typically contains:

| File | Description |
|---|---|
| `ue.log` | Detailed per-layer protocol trace (NAS/RRC/PHY/MAC/RLC/PDCP) |
| `stdout.log` | Console output: UE stats table, CBR traffic results, warnings |
| `amarisoft_ue.cfg` | JSON5 configuration: cell groups, UE list, sim events, `log_options` |
| `metrics.json` | JSON list of `stats` records, one per ~5 s: cumulative message counters, per-cell interval counts |
| `ps_info_lteue-avx2.txt` | Process CPU/memory snapshot (rarely needed) |

`metrics.json` may be empty in older runs. When `log_options` sets `all.level=warn`
(Retina runs), `ue.log` holds no PHY/NAS/RRC info records: its PRACH, NAS state and
RRC counts are 0 or undercounted, and the `metrics.json` counters (`prach`,
`nr_rrc_setup`, `5gs_nas_registration_accept`, ...) are the source of truth.

## Subtree layout

Debugging is symptom-first, so the docs split by role:

- **`troubleshooting/`** — per-procedure failure playbooks (failure markers /
  investigation checklist) for registration, data-session, handover. Start here
  from a symptom; each cites the expected-sequence `reference/` doc.
- **`reference/`** — lazily-loaded knowledge: format refs (`log-format.md`,
  `config-format.md`) and the expected-sequence procedure refs (sequence +
  vocabulary **only**; failure content lives in the paired `troubleshooting/`
  doc).
- top level — the entry point (`conventions.md`) and the three activity slots
  (`overview.md`, `query.md`, `investigate.md`) that the mode playbooks pull from.

The `investigate.md` § symptom → playbook dispatch table maps each symptom
to the `troubleshooting/` doc to load.

## Resolve & scope

The dispatcher runs `scripts/amari-ue/resolve.py`, which accepts a UE log file
(`ue.log`, or another name such as `ue0.log`, used as given), a run directory,
an `amarisoft-ue-N/` component dir, or a Retina `test_gnb[...]` dir; a directory
resolves to the `ue.log` of the latest run directory under it. It prints the
resolved run dir, the analysis artifacts present (the UE log, `stdout.log`,
`amarisoft_ue.cfg`), the **simulated UE count**, and a `verdict:` line; it exits
non-zero (`BAIL`) if no UE log is found, it is empty, or its first lines are not
a UE log (`#` header or `HH:MM:SS.mmm[uuu] [LAYER]` records). **Bail if the verdict
is not OK.** A directory with only `ue0.log`-style names bails; pass the file.

**UE count**: sum `ue_count` across every group in `amarisoft_ue.cfg` (one cfg
can hold many imsi/ue_count blocks — e.g. 500 IMSIs at `ue_count: 1` = 500 UEs).
Never infer it from the testbed's `amarisoft-ue-N` slot count — that's
container slots, not simulated UEs, and is commonly 1 even for hundreds of UEs.

**Scope** — when the input holds more than one UE component dir (e.g.
`amarisoft-ue-1` + `amarisoft-ue-2`, or one per test dir above several
`test_gnb[...]` dirs), `resolve.py` lists them by path and notes which one it
resolved to; scope explicitly to the UE you mean before going further.

## Efficiency rules (amari-ue)

- Size: `ue.log` can exceed 100k lines — never read it into context.
- `stdout.log` is short (30–100 lines) — safe to read in full.
- `amarisoft_ue.cfg` is short (100–150 lines) — safe to read in full.
- Scope by the 4-char hex UE ID when `ue_count > 1`.
