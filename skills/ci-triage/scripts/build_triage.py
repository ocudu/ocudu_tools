#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Merge a triage's decisions with the fetched data, check them, and render the triage table.

Usage:
    build_triage.py <pipeline_dir>

Reads fetch.json, groups.json (group_failures.py) and decisions.json from <pipeline_dir>. Writes triage.json and
triage.md there and prints the table. Exits 1, listing every problem, when decisions.json is incomplete or inconsistent, so it
can be fixed and the script run again.

decisions.json, written by the model, one entry per group of fetch.json; a failure ID entry overrides its group's
decision for that failure:
{
  "decisions": {
    "G1": {
      "error_type": "build | crash | assertion | timeout | setup | criteria",
      "key_error": "the most specific error line",
      "excerpt": "up to 30 lines of log around it",
      "verdict": "Matched | Check | No match | Infra",   # Infra: only, and always, for groups marked infra
      "issue": "group/project#iid",              # null for No match
      "reason": "one line: why this issue",
      "alternatives": [{"issue": "group/project#iid", "reason": "..."}],   # Check only
      "related": "group/project#iid",            # optional: a new issue links to it
      "new_issue": "A"                           # No match only: decisions with the same key share one new issue
    },
    "F7": {"...": "same fields, for failure F7 only"}
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
VERDICTS = ("Matched", "Check", "No match", "Infra")
_ISSUE_REF_RE = re.compile(r"^[\w.\-/]+#\d+$")
_MAX_EXCERPT_LINES = 30


def _check_ref(errors: list, fid: str, field: str, value) -> None:
    if value is not None and not (isinstance(value, str) and _ISSUE_REF_RE.match(value)):
        errors.append(f"{fid}: {field} must be 'group/project#iid', got {value!r}")


def with_groups(fetched: dict, grouped: dict) -> dict:
    """fetch.json with group_failures.py's groups, and each failure's group."""
    groups = grouped.get("groups") or []
    group_of = {fid: g["id"] for g in groups for fid in g["failures"]}
    return {**fetched, "groups": groups,
            "failures": [{**f, "group": group_of.get(f["id"])} for f in fetched["failures"]]}


def with_infra(fetched: dict, decisions: dict) -> dict:
    """Decisions with one for every infra group: verdict Infra, and the model's key_error if it gave one."""
    given = dict(decisions.get("decisions") or {})
    for g in fetched["groups"]:
        if g.get("infra"):
            given[g["id"]] = {"error_type": "setup", "key_error": f"infra: {g['infra']}", "verdict": "Infra",
                              **given.get(g["id"], {})}
    return {**decisions, "decisions": given}


def _id_order(key: str) -> tuple:
    return key[0], int(key[1:]) if key[1:].isdigit() else 0


def validate(fetched: dict, decisions: dict) -> list[str]:
    errors = []
    groups = {g["id"]: g for g in fetched["groups"]}
    failures = {f["id"] for f in fetched["failures"]}
    given = decisions.get("decisions") or {}
    for key in sorted(given.keys() - groups.keys() - failures, key=_id_order):
        errors.append(f"{key}: not a group or failure of fetch.json")
    for gid, g in groups.items():
        if gid in given:
            continue
        missing = [f for f in g["failures"] if f not in given]
        if len(missing) == len(g["failures"]):
            errors.append(f"{gid}: no decision")
        elif missing:
            errors.append(f"{gid}: no decision; {len(missing)} of its {len(g['failures'])} failures have none of their "
                          f"own")
    for f in fetched["failures"]:
        if f["group"] is None and f["id"] not in given:
            errors.append(f"{f['id']}: no decision (in no group of groups.json)")

    group_of = {f["id"]: f["group"] for f in fetched["failures"]}
    for key, d in given.items():
        if key not in groups and key not in failures:
            continue
        verdict = d.get("verdict")
        if verdict not in VERDICTS:
            errors.append(f"{key}: verdict must be one of {list(VERDICTS)}, got {verdict!r}")
            continue
        infra = (groups.get(key) or groups.get(group_of.get(key)) or {}).get("infra")
        if infra and verdict != "Infra":
            errors.append(f"{key}: GitLab failed it for {infra}, not its script: verdict must be Infra")
            continue
        if verdict == "Infra":
            if not infra:
                errors.append(f"{key}: Infra is only for groups marked infra")
            for field in ("issue", "alternatives", "related", "new_issue"):
                if d.get(field):
                    errors.append(f"{key}: Infra has no {field}")
            continue
        if d.get("error_type") not in ERROR_TYPES:
            errors.append(f"{key}: error_type must be one of {sorted(ERROR_TYPES)}, got {d.get('error_type')!r}")
        if not (d.get("key_error") or "").strip():
            errors.append(f"{key}: key_error is empty")
        _check_ref(errors, key, "issue", d.get("issue"))
        _check_ref(errors, key, "related", d.get("related"))
        for alt in d.get("alternatives") or []:
            _check_ref(errors, key, "alternatives[].issue", alt.get("issue"))

        if verdict == "No match":
            if d.get("issue"):
                errors.append(f"{key}: No match has no issue; use related to link one")
        else:
            if not d.get("issue"):
                errors.append(f"{key}: {verdict} needs an issue")
            if d.get("new_issue"):
                errors.append(f"{key}: new_issue is only for No match")
        if verdict != "Check" and d.get("alternatives"):
            errors.append(f"{key}: alternatives are only for Check")
    return errors


def _excerpt(decision: dict) -> str:
    lines = (decision.get("excerpt") or "").splitlines()
    return "\n".join(lines[-_MAX_EXCERPT_LINES:])


def build(fetched: dict, decisions: dict) -> dict:
    given = decisions["decisions"]
    failures = []
    for f in fetched["failures"]:
        key = f["id"] if f["id"] in given else f["group"]
        d = given[key]
        failures.append({
            "id": f["id"],
            "group": f["group"],
            "decision": key,
            "job": f["job"],
            "test": f.get("test"),
            "error_type": d["error_type"],
            "key_error": d["key_error"].strip(),
            "excerpt": _excerpt(d),
            "verdict": d["verdict"],
            "issue": d.get("issue") and {"ref": d["issue"], "reason": d.get("reason", "")},
            "alternatives": d.get("alternatives") or [],
            "related": d.get("related"),
            "new_issue": (d.get("new_issue") or key) if d["verdict"] == "No match" else None,
            "recent_failures": f.get("recent_failures"),
        })
    return {"pipeline": fetched["pipeline"], "failures": failures}


def recent_text(recent: dict | None) -> str:
    """GitLab's count of the test's failures on the default branch, every pipeline included."""
    return f"{recent['count']} in 14 days on {recent['branch']}" if recent else ""


def _issue_url(pipeline: dict, ref: str) -> str:
    project, iid = ref.split("#")
    return f"{re.match(r'https?://[^/]+', pipeline['web_url']).group()}/{project}/-/issues/{iid}"


_MAX_JOBS = 3


def _more(items: list, limit: int, noun: str) -> list:
    return items[:limit] + ([(f"+{len(items) - limit} more {noun}", None)] if len(items) > limit else [])


def rows(triage: dict) -> list[dict]:
    """One row per decision: a group, or a failure decided on its own. A cell is a list of (text, url) parts."""
    p = triage["pipeline"]
    by_decision: dict = {}
    for f in triage["failures"]:
        by_decision.setdefault(f["decision"], []).append(f)
    shared = Counter(fs[0]["new_issue"] for fs in by_decision.values() if fs[0]["new_issue"])
    out = []
    for members in by_decision.values():
        f = members[0]
        jobs = list({m["job"]["id"]: m["job"] for m in members}.values())
        job_parts = [(j["name"] + (" (allowed to fail)" if j.get("allow_failure") else ""), j["web_url"]) for j in jobs]
        tests = list(dict.fromkeys(m["test"] for m in members if m["test"]))
        if f["verdict"] == "Infra":
            issue = [("— (retry the job)", None)]
        elif f["issue"]:
            ref = f["issue"]["ref"]
            issue = [(ref, _issue_url(p, ref))] + [(f"alt: {a['issue']}", _issue_url(p, a["issue"]))
                                                   for a in f["alternatives"]]
        else:
            text = "new" + (f" {f['new_issue']}" if shared[f["new_issue"]] > 1 else "")
            issue = [(text, None)]
            if f["related"]:
                issue.append((f"related {f['related']}", _issue_url(p, f["related"])))
        recent = next((m["recent_failures"] for m in members if m["recent_failures"]), None)
        out.append({
            "Job": _more(job_parts, _MAX_JOBS, "jobs"),
            "Test": _more([(t, None) for t in tests], 1, "tests") if tests else [("—", None)],
            "Key failure": [(f["key_error"], None)],
            "Issue": issue,
            "Verdict": [(f["verdict"], None)],
            "Recent failures": [(recent_text(recent), None)],
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

    def md(col: str, parts: list) -> str:
        out = []
        for text, url in parts:
            text = text.replace("|", "\\|")
            if col == "Test" and text != "—" and not text.startswith("+"):
                text = f"`{text}`"
            elif col == "Job" and url:
                name, _, rest = text.partition(" (")
                text = f"[{name}]({url})" + (f" ({rest}" if rest else "")
            out.append(text)
        return ", ".join(out)

    lines += ["| " + " | ".join(COLUMNS) + " |", "|" + " --- |" * len(COLUMNS)]
    lines += ["| " + " | ".join(md(c, r[c]) for c in COLUMNS) + " |" for r in rows(triage)]
    return "\n".join(lines) + "\n"


_VERDICT_COLOR = {"Matched": "32", "Check": "33", "No match": "31", "Infra": "36"}


def render_terminal(triage: dict, width: int, links: bool, color: bool) -> str:
    """A box table for a terminal or a CI log; links as numbered footnotes, and clickable when `links`."""
    p = triage["pipeline"]
    sgr = (lambda code, t: f"\x1b[{code}m{t}\x1b[0m") if color else (lambda code, t: t)
    out = [sgr("1", f"Pipeline {p['id']} ({_title(p)})"), p["web_url"], ""]
    if not triage["failures"]:
        return "\n".join(out + ["No failed jobs in this pipeline."]) + "\n"

    # A cell is a list of (text, url) parts; each part starts on its own line, a link gets a footnote number.
    notes: dict[str, int] = {}
    table = []
    for r in rows(triage):
        table.append({c: [(text + (f" [{notes.setdefault(url, len(notes) + 1)}]" if url else ""), url)
                          for text, url in r[c]] for c in COLUMNS})

    # Fixed columns keep their width (capped); Test and Key failure share what is left.
    natural = {c: max(len(c), *(len(t) for row in table for t, _ in row[c])) for c in COLUMNS}
    widths = {c: min(natural[c], 40) for c in COLUMNS if c not in ("Test", "Key failure")}
    room = width - (3 * len(COLUMNS) + 1) - sum(widths.values())
    test, key = natural["Test"], natural["Key failure"]
    if test + key > room:
        test = min(test, max(12, room * test // (test + key)))
        key = max(12, room - test)
    widths.update({"Test": test, "Key failure": key})

    def cell_lines(col: str, parts: list) -> list[str]:
        styled = []
        for text, url in parts:
            for line in textwrap.wrap(text, widths[col], break_on_hyphens=False) or [""]:
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
        cells = [cell_lines(c, row[c]) for c in COLUMNS]
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
    if fetched["failures"]:
        fetched = with_groups(fetched, load("groups.json"))
        only_infra = all(g.get("infra") for g in fetched["groups"])
        decisions = load("decisions.json", required=not only_infra) or {"decisions": {}}
    else:
        fetched, decisions = with_groups(fetched, {}), {"decisions": {}}
    decisions = with_infra(fetched, decisions)

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
