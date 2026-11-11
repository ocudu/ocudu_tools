#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Turn a triage.json into GitLab writes: replies in issue triage threads, reopened issues, new issues.

Usage:
    post_triage.py <triage.json> [--dry-run] [--issue-project PROJECT] [--actions FILE]

With --dry-run, nothing is written: the planned actions are only printed (and saved with --actions). Planning reads
GitLab either way: issue states, triage threads, the issue template.

Per failure:
  Matched, issue open      reply in the issue's triage thread
  Matched, issue closed    reopen, then reply (an issue closed after the pipeline started counts as open)
  Check, issue open        reply, marked uncertain, with the alternatives
  Check, issue closed      new issue, related to the closed one
  No match                 new issue (related to `related` if set); failures with the same new_issue key share it
  Infra                    nothing: the runner or the cluster failed the job, retrying it is the fix

Failures are listed as a table, one row per decision and job; a long test list is collapsed under it.

Each issue has one triage thread, found by its first note's marker; each pipeline adds one reply, whose hidden marker
holds the pipeline ID and its failures. A pipeline already replied to is skipped, so running twice is harmless.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ci-triage" / "scripts"))

from build_triage import recent_text  # noqa: E402
from gitlab_api import (  # noqa: E402
    THREAD_MARKER, Client, GitLabError, encode_run_marker, project_path, triage_runs, triage_thread,
)

TEMPLATE_PATH = ".gitlab/issue_templates/triage.md"
THREAD_HEADER = (
    f"{THREAD_MARKER}\n## AI triage: CI occurrences\n\n"
    "Failures of scheduled pipelines matched to this issue are added as replies to this thread. "
    "Reply here if a match is wrong; the next triage reads the replies."
)
_REF_RE = re.compile(r"^(.+)#(\d+)$")
_MAX_TITLE = 120
_RECENT_NEW_ISSUE = timedelta(days=2)
OPENED_BY = "Opened by the AI triage of a scheduled pipeline."   # in every new issue; found by search
_MAX_TESTS = 500    # tests in a collapsed list
_MAX_MARKED = 50    # failures kept in a reply's hidden marker


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def short_test(failure: dict) -> str:
    """test_gnb[singleue.ping.drx] -> ping.drx; the job name when there is no test."""
    test = failure["test"]
    if not test:
        return failure["job"]["name"]
    m = re.search(r"\[(.+)\]$", test)
    parts = (m.group(1) if m else test).split(".")
    return ".".join(parts[1:] if len(parts) > 2 else parts)


def tests_label(tests: list[str]) -> str:
    """One test as is; several by their common part (paging.a, paging.b -> paging), else the first and a count."""
    tests = list(dict.fromkeys(tests))
    if len(tests) == 1:
        return tests[0]
    common = []
    for parts in zip(*(t.split(".") for t in tests)):
        if len(set(parts)) > 1:
            break
        common.append(parts[0])
    return ".".join(common) if common else f"{tests[0]} and {len(tests) - 1} more"


def issue_title(failures: list[dict]) -> str:
    """<problem> - <test>, the test left out when it doesn't fit."""
    first = failures[0]
    title = first.get("title") or first["key_error"]
    where = " - " + tests_label([short_test(f) for f in failures])
    if len(title) + len(where) <= _MAX_TITLE:
        return title + where
    return title if len(title) <= _MAX_TITLE else title[:_MAX_TITLE - 1].rstrip() + "…"


def split_ref(ref: str) -> tuple[str, int]:
    m = _REF_RE.match(ref)
    return m.group(1), int(m.group(2))


def short_ref(ref: str, home: str) -> str:
    project, iid = split_ref(ref)
    return f"#{iid}" if project == home else ref


def _job_link(job: dict) -> str:
    return f"[{job['name']}]({job['web_url']})" + (" (allowed to fail)" if job.get("allow_failure") else "")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def failures_table(failures: list[dict], home: str) -> list[str]:
    """One row per decision and job; a long test list goes into a collapsed block under the table."""
    rows: dict = {}
    for f in failures:
        rows.setdefault((f.get("decision", f["id"]), f["job"]["id"]), []).append(f)
    lines = ["| Job | Test | Failure | Failed in 14 days |", "| --- | --- | --- | --- |"]
    details, notes = [], {}
    for members in rows.values():
        f = members[0]
        tests = list(dict.fromkeys(m["test"] for m in members if m["test"]))
        if not tests:
            test = "—"
        elif len(tests) == 1:
            test = f"`{_cell(tests[0])}`"
        else:
            test = f"`{_cell(tests[0])}` +{len(tests) - 1} more"
            listed = tests[:_MAX_TESTS]
            details += ["", f"<details><summary>{f['job']['name']}: {len(tests)} tests</summary>", ""]
            details += [f"- `{t}`" for t in listed]
            if len(tests) > len(listed):
                details.append(f"- … {len(tests) - len(listed)} more")
            details += ["", "</details>"]
        recent = next((m["recent_failures"] for m in members if m.get("recent_failures")), None)
        count = f"{recent['count']} on {recent['branch']}" if recent else ""
        failure = _cell(f["key_error"]) + (" · *uncertain match*" if f["verdict"] == "Check" else "")
        lines.append(f"| {_job_link(f['job'])} | {test} | {failure} | {count} |")
        if f["verdict"] == "Check":
            alts = ", ".join(short_ref(a["issue"], home) for a in f["alternatives"])
            notes[f["issue"]["reason"]] = alts
    for reason, alts in notes.items():
        lines += ["", f"**Uncertain match**: {reason}" + (f"; alternatives: {alts}" if alts else "")]
    return lines + details


def reply_body(pipeline: dict, failures: list[dict], home: str, action: str = "reply") -> str:
    """action: what this pipeline did to the issue, kept in the marker: new, reopen or reply."""
    date = (pipeline.get("created_at") or "")[:10]
    occurrences = [{"job": f["job"]["name"], "test": f["test"], "key_error": f["key_error"]} for f in failures]
    marker = encode_run_marker({
        "pipeline": pipeline["id"],
        "date": date,
        "action": action,
        "failures": occurrences[:_MAX_MARKED],
        **({"more": len(occurrences) - _MAX_MARKED} if len(occurrences) > _MAX_MARKED else {}),
    })
    link = f"[pipeline {pipeline['id']}]({pipeline['web_url']})"
    head = " · ".join(x for x in (date, pipeline.get("schedule"), link) if x)
    lines = [marker]
    if action == "reopen":
        lines.append("**Reopened: seen again.**\n")
    lines += [head, ""] + failures_table(failures, home)
    return "\n".join(lines)


def parse_quick_actions(template: str) -> tuple[str, list[str], list[str]]:
    """Template without its quick-action lines, plus the labels and assignee usernames they set."""
    body, labels, assignees = [], [], []
    for line in template.splitlines():
        if not line.startswith("/"):
            body.append(line)
            continue
        cmd = None
        for token in re.findall(r'~"[^"]+"|\S+', line):
            if token.startswith("/"):
                cmd = token[1:]
            elif cmd == "label" and token.startswith("~"):
                labels.append(token[1:].strip('"'))
            elif cmd == "assign" and token.startswith("@"):
                assignees.append(token[1:])
    return "\n".join(body).rstrip() + "\n", labels, assignees


def fill_template(template: str, failures: list[dict]) -> str:
    """Fill the triage template's sections; unknown sections are kept as they are."""
    first = failures[0]
    summary = "\n".join(failures_table(failures, ""))
    summary += f"\n\n{OPENED_BY}"
    if first.get("related"):
        summary += f" Related to {first['related']}."
    logs = "````text\n" + (first.get("excerpt") or first["key_error"]) + "\n````"
    links = "\n".join(dict.fromkeys(f"- [{f['job']['name']}]({f['job']['web_url']})" for f in failures))

    out, section = [], None
    for line in template.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            section = ("summary" if heading.startswith("summary") else
                       "logs" if "log" in heading else
                       "links" if "ci link" in heading else None)
            out += [line, "", {"summary": summary, "logs": logs, "links": links}.get(section, ""), ""] \
                if section else [line]
            continue
        if section is None:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


class Planner:
    def __init__(self, client: Client, triage: dict, issue_project: str):
        self.client = client
        self.triage = triage
        self.pipeline = triage["pipeline"]
        self.home = issue_project
        self._issues: dict = {}
        self._discussions: dict = {}
        self._recent_new: list | None = None

    def issue(self, ref: str) -> dict:
        if ref not in self._issues:
            project, iid = split_ref(ref)
            self._issues[ref] = self.client.get(f"{project_path(project)}/issues/{iid}")
        return self._issues[ref]

    def thread(self, ref: str) -> dict | None:
        if ref not in self._discussions:
            project, iid = split_ref(ref)
            self._discussions[ref] = triage_thread(
                self.client.get_all(f"{project_path(project)}/issues/{iid}/discussions"))
        return self._discussions[ref]

    def closed_before_pipeline(self, ref: str) -> bool:
        """Closed before the pipeline started. Closed later, the failure predates the close (and maybe the fix), so
        the issue counts as open: reply, but don't reopen it or open another."""
        issue = self.issue(ref)
        if issue["state"] != "closed":
            return False
        closed_at, started = issue.get("closed_at"), self.pipeline.get("created_at")
        return not (closed_at and started and _parse_time(closed_at) > _parse_time(started))

    def recent_triage_issue(self, failures: list[dict]) -> str | None:
        """An issue the triage opened lately, with any token, for one of the same job and test: another pipeline got
        there first."""
        if self._recent_new is None:
            since = datetime.now(timezone.utc) - _RECENT_NEW_ISSUE
            candidates = self.client.get(f"{project_path(self.home)}/issues", {
                "search": OPENED_BY, "in": "description", "state": "opened", "created_after": since.isoformat(),
                "per_page": 100})
            self._recent_new = []
            for issue in candidates:
                ref = issue["references"]["full"]
                self._issues.setdefault(ref, issue)
                seen = {(f.get("job"), f.get("test")) for run in triage_runs(self.thread(ref))
                        for f in run.get("failures", [])}
                self._recent_new.append((ref, seen))
        wanted = {(f["job"]["name"], f["test"]) for f in failures}
        return next((ref for ref, seen in self._recent_new if seen & wanted), None)

    def plan(self) -> list[dict]:
        replies: dict = {}       # ref -> failures
        reopen: set = set()
        new: dict = {}           # key -> failures
        unmatched: dict = {}     # new_issue key -> failures
        for f in self.triage["failures"]:
            if f["verdict"] == "Infra":
                continue
            if f["issue"]:
                ref = f["issue"]["ref"]
                closed = self.closed_before_pipeline(ref)
                if f["verdict"] == "Check" and closed:
                    new.setdefault(f"check:{ref}", []).append({**f, "related": ref})
                    continue
                if closed:
                    reopen.add(ref)
                replies.setdefault(ref, []).append(f)
            else:
                unmatched.setdefault(f["new_issue"], []).append(f)
        for key, failures in unmatched.items():
            existing = self.recent_triage_issue(failures)
            if existing:
                replies.setdefault(existing, []).extend(failures)
            else:
                new[f"new:{key}"] = failures

        actions = []
        for ref, failures in replies.items():
            actions += self._reply_actions(ref, failures, ref in reopen)
        for key, failures in new.items():
            actions += self._new_issue_actions(key, failures)
        return actions

    def _reply_actions(self, ref: str, failures: list[dict], reopened: bool) -> list[dict]:
        thread = self.thread(ref)
        if any(run.get("pipeline") == self.pipeline["id"] for run in triage_runs(thread)):
            return [{"action": "skip", "issue": ref, "reason": f"pipeline {self.pipeline['id']} already replied"}]
        actions = []
        if reopened:
            actions.append({"action": "reopen", "issue": ref})
        if not thread:
            actions.append({"action": "create_thread", "issue": ref, "body": THREAD_HEADER})
        actions.append({"action": "reply", "issue": ref, "discussion": thread["id"] if thread else None,
                        "body": reply_body(self.pipeline, failures, self.home, "reopen" if reopened else "reply")})
        return actions

    def _new_issue_actions(self, key: str, failures: list[dict]) -> list[dict]:
        project = self.client.get(project_path(self.home))
        raw = self.client.get_raw(f"{project_path(self.home)}/repository/files/{TEMPLATE_PATH.replace('/', '%2F')}/raw",
                                  {"ref": project["default_branch"]}).decode()
        template, labels, assignees = parse_quick_actions(raw)
        first = failures[0]
        actions = [{
            "action": "create_issue",
            "issue": key,
            "project": self.home,
            "title": issue_title(failures),
            "description": fill_template(template, failures),
            "labels": labels,
            "assignees": assignees,
        }]
        if first.get("related"):
            actions.append({"action": "link", "issue": key, "related": first["related"]})
        actions.append({"action": "create_thread", "issue": key, "body": THREAD_HEADER})
        actions.append({"action": "reply", "issue": key, "discussion": None,
                        "body": reply_body(self.pipeline, failures, self.home, "new")})
        return actions


def execute(client: Client, actions: list[dict], log) -> list[dict]:
    created: dict = {}   # placeholder key -> real ref
    threads: dict = {}   # ref -> discussion id
    for a in actions:
        ref = created.get(a["issue"], a["issue"])
        if a["action"] == "skip":
            log(f"skip {ref}: {a['reason']}")
            continue
        if a["action"] == "create_issue":
            ids = []
            for username in a["assignees"]:
                users = client.get("users", {"username": username})
                ids += [u["id"] for u in users[:1]]
            issue = client.post(f"{project_path(a['project'])}/issues", {
                "title": a["title"], "description": a["description"],
                "labels": ",".join(a["labels"]), "assignee_ids": ids})
            created[a["issue"]] = issue["references"]["full"]
            a["result"] = issue["web_url"]
            log(f"created {issue['web_url']}")
            continue
        project, iid = split_ref(ref)
        base = f"{project_path(project)}/issues/{iid}"
        if a["action"] == "reopen":
            client.put(base, {"state_event": "reopen"})
        elif a["action"] == "link":
            target_project, target_iid = split_ref(a["related"])
            target = client.get(f"{project_path(target_project)}/issues/{target_iid}")
            client.post(f"{base}/links", {"target_project_id": target["project_id"],
                                          "target_issue_iid": target_iid, "link_type": "relates_to"})
        elif a["action"] == "create_thread":
            threads[ref] = client.post(f"{base}/discussions", {"body": a["body"]})["id"]
        elif a["action"] == "reply":
            discussion = a["discussion"] or threads[ref]
            client.post(f"{base}/discussions/{discussion}/notes", {"body": a["body"]})
        a["result"] = "done"
        log(f"{a['action']} {ref}")
    return actions


def main():
    parser = argparse.ArgumentParser(description="Write a triage to GitLab issues")
    parser.add_argument("triage", type=Path, help="triage.json from build_triage.py")
    parser.add_argument("--dry-run", action="store_true", help="Only print the plan; write nothing")
    parser.add_argument("--issue-project", default=os.environ.get("ISSUE_PROJECT", "ocudu/ocudu"),
                        help="Project new issues are opened in (default: $ISSUE_PROJECT, else ocudu/ocudu)")
    parser.add_argument("--actions", type=Path, help="Also save the actions as JSON to this file")
    parser.add_argument("--base-url", default="https://gitlab.com")
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""),
                        help="GitLab access token (default: $GITLAB_TOKEN)")
    args = parser.parse_args()

    def log(*msg):
        print(*msg, file=sys.stderr)

    triage = json.loads(args.triage.read_text(encoding="utf-8"))
    client = Client(args.base_url, args.token)
    try:
        actions = Planner(client, triage, args.issue_project).plan()
        if not args.dry_run:
            execute(client, actions, log)
    except GitLabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.actions:
        args.actions.write_text(json.dumps(actions, indent=2), encoding="utf-8")
    print(json.dumps(actions, indent=2))


if __name__ == "__main__":
    main()
