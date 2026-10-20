# viavi conventions

Type-specific deltas for `viavi` artifacts (the VIAVI RU-simulator command log).
The shared resolve / efficiency / memory flow lives in the skill `SKILL.md`; this
file carries only what is particular to VIAVI logs.

A VIAVI test produces a single artifact in two forms:

| File | Description |
|---|---|
| `YYMMDD_HHMMSS_Command_Log<NNN>.txt` | The command/event log — commands sent, `C:` responses, and the `I: CMPI` runtime event stream |
| `YYMMDD_HHMMSS_Command_Log<NNN>.zip` | A compressed copy of the **identical** `.txt` — not a distinct artifact |

The VIAVI tester emulates the RU, RF channel, NR UEs, and core; this log is the
control/observation channel for an OCUDU test, not an OCUDU app log. See
`reference/log-format.md` for the full grammar.

## Subtree layout

- **`troubleshooting/`** — failure playbooks: per-procedure (random-access,
  ue-attach) plus the throughput diagnostic (`throughput.md`). Start here from a
  symptom; each per-procedure doc cites its expected-sequence `reference/` doc.
- **`reference/`** — lazily-loaded knowledge: the format ref (`log-format.md`) and
  the expected-sequence / vocabulary procedure refs (sequence + vocabulary
  **only**; failure content lives in the paired `troubleshooting/` doc). Note
  `throughput.md` is a diagnostic with no `reference/` sibling.
- top level — the entry point (`conventions.md`) and the three activity slots
  (`overview.md`, `query.md`, `investigate.md`) that the mode playbooks pull from.

The `investigate.md` § symptom → playbook dispatch table maps each symptom
to the `troubleshooting/` doc to load.

## Resolve & scope

The dispatcher runs `scripts/viavi/resolve.py`, which accepts the `.txt`, the
`.zip`, or a directory containing either, and resolves to the command-log file
(newest basename if several; a `.txt` is preferred over its identical `.zip`).
It confirms the file is non-empty and readable (a broken `.zip` bails) and
carries the VIAVI signature (`RSET` / `C: RSET 0x00 Ok` / `I: CMPI`, or for a
log cut before the RSET `I: TMAE 0x` / `C: PORT|CONNECT|ABOT 0x`, in the first
lines), then prints the path, format, size, and
a `verdict:` line. **Bail if the verdict is not OK.**

**Scope** — a single command log already aggregates *all* simulated UEs (often
100+). Scope queries by `UE Id` early; the per-UE
cross-product over a multi-hour run is large.

## Efficiency rules (viavi)

- Size: hundreds of thousands of lines (observed: 655k lines / 55 MB) — never read
  it into context.
- The file has **CRLF** endings and some **non-UTF-8 bytes** — always grep with
  `LC_ALL=C grep -a`; the helper scripts already strip CR and use
  `errors="replace"`.
- Read the `.zip` in place (the scripts do, via `zipfile`); don't extract a
  55 MB `.txt` to disk just to grep it.
- Records span several lines (untimestamped `Cell Info:` bodies, GETSTATS rows
  that repeat their header's timestamp); use `viavi_log_search.py` (block-aware)
  rather than line greps when you need whole records.
