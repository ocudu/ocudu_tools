#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Merge a triage's decisions with the fetched data, check them, and render the triage table.

Usage:
    build_triage.py <pipeline_dir>

Reads fetch.json and decisions.json from <pipeline_dir>. Writes triage.json and triage.md
there and prints the table. Exits 1, listing every problem, when decisions.json is incomplete or inconsistent, so it
can be fixed and the script run again.

decisions.json, written by the model, one entry per failure ID of fetch.json:
{
  "failures": {
    "F1": {
      "error_type": "build | crash | assertion | timeout | setup | criteria",
      "key_error": "the most specific error line",
      "excerpt": "up to 30 lines of log around it",
      "verdict": "Matched | Check | No match",
      "issue": "group/project#iid",              # null for No match
      "reason": "one line: why this issue",
      "alternatives": [{"issue": "group/project#iid", "reason": "..."}],   # Check only
      "related": "group/project#iid",            # optional: a new issue links to it
      "new_issue": "A"                           # No match only: failures with the same key share one new issue
    }
  }
}
"""

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
from collections import Counter
from pathlib import Path

ERROR_TYPES = {"build", "crash", "assertion", "timeout", "setup", "criteria"}
VERDICTS = ("Matched", "Check", "No match")
_ISSUE_REF_RE = re.compile(r"^[\w.\-/]+#\d+$")
_MAX_EXCERPT_LINES = 30


def _check_ref(errors: list, fid: str, field: str, value) -> None:
    if value is not None and not (isinstance(value, str) and _ISSUE_REF_RE.match(value)):
        errors.append(f"{fid}: {field} must be 'group/project#iid', got {value!r}")


def validate(fetched: dict, decisions: dict) -> list[str]:
    errors = []
    wanted = {f["id"] for f in fetched["failures"]}
    given = decisions.get("failures") or {}
    for fid in sorted(wanted - given.keys(), key=lambda x: int(x[1:])):
        errors.append(f"{fid}: no decision")
    for fid in sorted(given.keys() - wanted):
        errors.append(f"{fid}: not a failure of fetch.json")

    for fid, d in given.items():
        if fid not in wanted:
            continue
        verdict = d.get("verdict")
        if verdict not in VERDICTS:
            errors.append(f"{fid}: verdict must be one of {sorted(VERDICTS)}, got {verdict!r}")
            continue
        if d.get("error_type") not in ERROR_TYPES:
            errors.append(f"{fid}: error_type must be one of {sorted(ERROR_TYPES)}, got {d.get('error_type')!r}")
        if not (d.get("key_error") or "").strip():
            errors.append(f"{fid}: key_error is empty")
        _check_ref(errors, fid, "issue", d.get("issue"))
        _check_ref(errors, fid, "related", d.get("related"))
        for alt in d.get("alternatives") or []:
            _check_ref(errors, fid, "alternatives[].issue", alt.get("issue"))

        if verdict == "No match":
            if d.get("issue"):
                errors.append(f"{fid}: No match has no issue; use related to link one")
        else:
            if not d.get("issue"):
                errors.append(f"{fid}: {verdict} needs an issue")
            if d.get("new_issue"):
                errors.append(f"{fid}: new_issue is only for No match")
        if verdict != "Check" and d.get("alternatives"):
            errors.append(f"{fid}: alternatives are only for Check")
    return errors


def _excerpt(decision: dict) -> str:
    lines = (decision.get("excerpt") or "").splitlines()
    return "\n".join(lines[-_MAX_EXCERPT_LINES:])


def build(fetched: dict, decisions: dict) -> dict:
    failures = []
    for f in fetched["failures"]:
        d = decisions["failures"][f["id"]]
        failures.append({
            "id": f["id"],
            "job": f["job"],
            "test": f.get("test"),
            "error_type": d["error_type"],
            "key_error": d["key_error"].strip(),
            "excerpt": _excerpt(d),
            "verdict": d["verdict"],
            "issue": d.get("issue") and {"ref": d["issue"], "reason": d.get("reason", "")},
            "alternatives": d.get("alternatives") or [],
            "related": d.get("related"),
            "new_issue": (d.get("new_issue") or f["id"]) if d["verdict"] == "No match" else None,
            "recent_failures": f.get("recent_failures"),
        })
    return {"pipeline": fetched["pipeline"], "failures": failures}


def recent_text(recent: dict | None) -> str:
    """GitLab's count of the test's failures on the default branch, every pipeline included."""
    return f"{recent['count']} in 14 days on {recent['branch']}" if recent else ""


def _issue_url(pipeline: dict, ref: str) -> str:
    project, iid = ref.split("#")
    return f"{re.match(r'https?://[^/]+', pipeline['web_url']).group()}/{project}/-/issues/{iid}"


def rows(triage: dict) -> list[dict]:
    """One row per failure; cells are (text, url) pairs, url None when there is no link."""
    p = triage["pipeline"]
    shared = Counter(f["new_issue"] for f in triage["failures"] if not f["issue"])
    out = []
    for f in triage["failures"]:
        job = f["job"]["name"] + (" (allowed to fail)" if f["job"].get("allow_failure") else "")
        if f["issue"]:
            ref = f["issue"]["ref"]
            issue = (ref + ("; alt: " + ", ".join(a["issue"] for a in f["alternatives"]) if f["alternatives"] else ""),
                     _issue_url(p, ref))
        else:
            text = "new" + (f" {f['new_issue']}" if shared[f["new_issue"]] > 1 else "")
            text += f", related {f['related']}" if f["related"] else ""
            issue = (text, _issue_url(p, f["related"]) if f["related"] else None)
        out.append({
            "Job": (job, f["job"]["web_url"]),
            "Test": (f["test"] or "—", None),
            "Key failure": (f["key_error"], None),
            "Issue": issue,
            "Verdict": (f["verdict"], None),
            "Recent failures": (recent_text(f["recent_failures"]), None),
        })
    return out


COLUMNS = ["Job", "Test", "Key failure", "Issue", "Verdict", "Recent failures"]


def _title(p: dict) -> str:
    date = (p.get("created_at") or "")[:10]
    return ", ".join(x for x in (p.get("ref"), p.get("schedule"), date) if x)


def render(triage: dict) -> str:
    p = triage["pipeline"]
    lines = [f"## [{p['id']}]({p['web_url']}) ({_title(p)})", ""]
    if not triage["failures"]:
        lines.append("No failed jobs in this pipeline.")
        return "\n".join(lines) + "\n"

    def md(col: str, cell: tuple) -> str:
        text, url = cell
        text = text.replace("|", "\\|")
        if col == "Test" and text != "—":
            text = f"`{text}`"
        if url and col == "Job":
            name, _, rest = text.partition(" (")
            return f"[{name}]({url})" + (f" ({rest}" if rest else "")
        return text

    lines += ["| " + " | ".join(COLUMNS) + " |", "|" + " --- |" * len(COLUMNS)]
    lines += ["| " + " | ".join(md(c, r[c]) for c in COLUMNS) + " |" for r in rows(triage)]
    return "\n".join(lines) + "\n"


_VERDICT_COLOR = {"Matched": "32", "Check": "33", "No match": "31"}


def render_terminal(triage: dict, width: int, links: bool, color: bool) -> str:
    """A box table for a terminal or a CI log; links as numbered footnotes, and clickable when `links`."""
    p = triage["pipeline"]
    sgr = (lambda code, t: f"\x1b[{code}m{t}\x1b[0m") if color else (lambda code, t: t)
    out = [sgr("1", f"Pipeline {p['id']} ({_title(p)})"), p["web_url"], ""]
    if not triage["failures"]:
        return "\n".join(out + ["No failed jobs in this pipeline."]) + "\n"

    notes: dict[str, int] = {}
    table = []
    for r in rows(triage):
        row = {}
        for c in COLUMNS:
            text, url = r[c]
            if url:
                text += f" [{notes.setdefault(url, len(notes) + 1)}]"
            row[c] = (text, url)
        table.append(row)

    # Fixed columns keep their width (capped); Test and Key failure share what is left.
    natural = {c: max(len(c), *(len(row[c][0]) for row in table)) for c in COLUMNS}
    widths = {c: min(natural[c], 40) for c in COLUMNS if c not in ("Test", "Key failure")}
    room = width - (3 * len(COLUMNS) + 1) - sum(widths.values())
    test, key = natural["Test"], natural["Key failure"]
    if test + key > room:
        test = min(test, max(12, room * test // (test + key)))
        key = max(12, room - test)
    widths.update({"Test": test, "Key failure": key})

    def cell_lines(col: str, text: str, url: str | None) -> list[str]:
        wrapped = textwrap.wrap(text, widths[col], break_on_hyphens=False) or [""]
        styled = []
        for line in wrapped:
            pad = " " * (widths[col] - len(line))
            if url and links:
                line = f"\x1b]8;;{url}\x1b\\{line}\x1b]8;;\x1b\\"
            if col == "Verdict":
                line = sgr(_VERDICT_COLOR.get(text, "0"), line)
            styled.append(line + pad)
        return styled

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (widths[c] + 2) for c in COLUMNS) + right

    out.append(rule("┌", "┬", "┐"))
    out.append("│ " + " │ ".join(sgr("1", c.ljust(widths[c])) for c in COLUMNS) + " │")
    for row in table:
        out.append(rule("├", "┼", "┤"))
        cells = [cell_lines(c, *row[c]) for c in COLUMNS]
        for i in range(max(map(len, cells))):
            out.append("│ " + " │ ".join(cell[i] if i < len(cell) else " " * widths[c]
                                          for c, cell in zip(COLUMNS, cells)) + " │")
    out.append(rule("└", "┴", "┘"))
    out += [""] + [f"[{n}] {url}" for url, n in notes.items()]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Build triage.json and the triage table from decisions.json")
    parser.add_argument("pipeline_dir", type=Path, help="Directory written by fetch_pipeline.py")
    parser.add_argument("--show", action="store_true",
                        help="Only print the triage.json already built, as a table for a terminal or CI log")
    args = parser.parse_args()

    if args.show:
        triage = json.loads((args.pipeline_dir / "triage.json").read_text(encoding="utf-8"))
        tty = sys.stdout.isatty()
        color = (tty or bool(os.environ.get("CI"))) and not os.environ.get("NO_COLOR")
        print(render_terminal(triage, shutil.get_terminal_size((160, 24)).columns, links=tty, color=color), end="")
        return

    def load(name: str, required: bool = True) -> dict:
        path = args.pipeline_dir / name
        if not path.exists():
            if required:
                print(f"ERROR: {path} not found", file=sys.stderr)
                sys.exit(1)
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    fetched = load("fetch.json")
    decisions = load("decisions.json") if fetched["failures"] else {"failures": {}}

    errors = validate(fetched, decisions)
    if errors:
        print("decisions.json has problems; fix them and run again:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    triage = build(fetched, decisions)
    table = render(triage)
    (args.pipeline_dir / "triage.json").write_text(json.dumps(triage, indent=2), encoding="utf-8")
    (args.pipeline_dir / "triage.md").write_text(table, encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
