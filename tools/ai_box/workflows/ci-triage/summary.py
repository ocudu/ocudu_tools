#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Status of the scheduled pipelines of a GitLab group over the last hours, from what the AI triage wrote to issues.

Usage:
    summary.py [--hours 24] [--group ocudu] [--ignore PROJECT[:SCHEDULE]]...

Per schedule, its latest pipeline that finished in the window, linked (IGNORE lists the schedules left out):
  :white_check_mark:   passed
  :new:                an issue the triage opened for it
  :recycle:            a closed issue the triage reopened for it
  :repeat:             an open issue it was matched to again
  :construction:       failed only for infrastructure reasons (runner, cluster): not triaged
  :warning:            failed, but no triage reply found
  :heavy_minus_sign:   canceled
  :arrows_counterclockwise:   running (or jobs being retried), with the issues an earlier triage of it found
Then the issues labelled `bug` (not `bug::ci`) opened in the window, which may be related.

Prints Slack-flavoured text to paste. Authentication: $GITLAB_TOKEN env var or --token.
"""

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ci-triage" / "scripts"))

from gitlab_api import Client, GitLabError, triage_runs, triage_thread  # noqa: E402

FINISHED = {"success", "failed", "canceled"}
RUNNING = {"running", "pending"}   # also a finished pipeline with jobs being retried
SCRIPT_REASONS = {None, "script_failure"}
ACTION_EMOJI = {"new": ":new:", "reopen": ":recycle:", "reply": ":repeat:"}
ACTION_ORDER = ["new", "reopen", "reply"]
# Schedules left out: "project" for all of a project's, or "project:schedule" for one.
IGNORE = ["ocudu/ocudu_test_report"]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ignored(project: dict, schedule: dict, ignore: list[str]) -> bool:
    path = project["path_with_namespace"]
    return path in ignore or f"{path}:{schedule['description']}" in ignore


def scheduled_pipelines(client: Client, group: str, since: datetime, ignore: list[str]) -> list[dict]:
    """Per schedule of the group's projects, its latest pipeline that finished, or ran, since `since`."""
    projects = client.get_all(f"groups/{group.replace('/', '%2F')}/projects",
                              {"include_subgroups": "true", "archived": "false", "per_page": 100})

    def schedules(project: dict) -> list[tuple[dict, dict]]:
        try:
            found = client.get(f"projects/{project['id']}/pipeline_schedules", {"per_page": 100})
        except GitLabError:
            return []   # no access to the project's schedules
        return [(project, s) for s in found if not ignored(project, s, ignore)]

    def latest(item: tuple[dict, dict]) -> dict | None:
        project, schedule = item
        pipelines = client.get(f"projects/{project['id']}/pipeline_schedules/{schedule['id']}/pipelines",
                               {"per_page": 20, "sort": "desc"})
        done = [p for p in pipelines if p["status"] in FINISHED | RUNNING and _time(p["updated_at"]) >= since]
        if not done:
            return None
        p = max(done, key=lambda p: p["id"])
        return {"project": project, "schedule": schedule["description"], "pipeline": p}

    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = [pair for found in pool.map(schedules, projects) for pair in found]
        return [r for r in pool.map(latest, pairs) if r]


def triage_replies(client: Client, group: str, since: datetime, pipelines: set[int]) -> dict[int, list[dict]]:
    """Per pipeline, the issues the triage replied to for it: {issue, action}."""
    issues = client.get_all(f"groups/{group.replace('/', '%2F')}/issues",
                            {"updated_after": since.isoformat(), "state": "all", "per_page": 100})
    issues = [i for i in issues if i.get("user_notes_count")]

    def runs(issue: dict) -> list[tuple[dict, dict, bool]]:
        discussions = client.get_all(f"projects/{issue['project_id']}/issues/{issue['iid']}/discussions")
        found = triage_runs(triage_thread(discussions))
        return [(issue, run, n == 0) for n, run in enumerate(found)]

    out: dict = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found in pool.map(runs, issues):
            for issue, run, first in found:
                if run.get("pipeline") not in pipelines:
                    continue
                # Replies from before the marker named the action: new if the issue is no older than the run.
                action = run.get("action") or ("new" if first and issue["created_at"][:10] >= run.get("date", "")
                                               else "reply")
                out.setdefault(run["pipeline"], []).append({"issue": issue, "action": action})
    return out


def infra_only(client: Client, project_id: int, pipeline_id: int) -> bool:
    jobs = client.get_all(f"projects/{project_id}/pipelines/{pipeline_id}/jobs", {"scope[]": "failed"})
    return bool(jobs) and all(j.get("failure_reason") not in SCRIPT_REASONS for j in jobs)


def other_bugs(client: Client, group: str, since: datetime) -> list[dict]:
    issues = client.get_all(f"groups/{group.replace('/', '%2F')}/issues",
                            {"labels": "bug", "created_after": since.isoformat(), "state": "all", "per_page": 100})
    return [i for i in issues if "bug::ci" not in i.get("labels", [])]


_MAX_TITLE = 80


def render(results: list[dict], replies: dict, infra: dict, bugs: list[dict]) -> str:
    """Header, then the passing schedules on one line, then one block per project with something to look at."""
    def issue_line(issue: dict, project_id: int | None) -> str:
        ref = issue["references"]["full"]
        ref = f"#{issue['iid']}" if issue["project_id"] == project_id else ref.split("/", 1)[-1]
        title = re.sub(r"^\[CI\]\s*", "", issue["title"])
        title = title if len(title) <= _MAX_TITLE else title[:_MAX_TITLE - 1].rstrip() + "…"
        return f"[{ref}]({issue['web_url']}) {title}"

    def link(text: str, r: dict) -> str:
        return f"[{text}]({r['pipeline']['web_url']})"

    passing: dict = {}   # project path -> [result]
    blocks: dict = {}    # project path -> [lines]
    for r in sorted(results, key=lambda r: (r["project"]["path_with_namespace"], r["schedule"].lower())):
        project, p = r["project"], r["pipeline"]
        if p["status"] == "success":
            passing.setdefault(project["path"], []).append(r)
            continue
        lines = blocks.setdefault(project["path"], [])
        head = f"- {link(r['schedule'], r)}"
        found = replies.get(p["id"], [])
        if p["status"] == "canceled":
            lines.append(f"{head}: :heavy_minus_sign: canceled")
            continue
        if p["status"] in RUNNING:
            head += ": :arrows_counterclockwise: running" + (", triaged before:" if found else "")
        elif not found:
            head += ": " + (":construction: infra failures only (retry)" if infra.get(p["id"])
                            else ":warning: failed, no triage found")
        lines.append(head)
        best: dict = {}
        for item in found:
            key = item["issue"]["id"]
            if key not in best or ACTION_ORDER.index(item["action"]) < ACTION_ORDER.index(best[key]["action"]):
                best[key] = item
        for item in sorted(best.values(), key=lambda i: ACTION_ORDER.index(i["action"])):
            lines.append(f"  - {ACTION_EMOJI[item['action']]} {issue_line(item['issue'], project['id'])}")

    out = [f"**AI CI status ({datetime.now(timezone.utc):%Y-%m-%d})**", ""]
    if passing:
        out += [":white_check_mark: " + " · ".join(
            f"{path} " + ", ".join(link(r["schedule"], r) for r in rs) for path, rs in passing.items()), ""]
    for path, lines in blocks.items():
        out += [f"*{path}*"] + lines + [""]
    if bugs:
        out += ["*Other new bugs* (not CI)"] + [f"- :beetle: {issue_line(i, None)}" for i in bugs] + [""]
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Status of a group's scheduled pipelines, from the AI triage")
    parser.add_argument("--hours", type=float, default=24, help="Window, in hours (default 24)")
    parser.add_argument("--group", default="ocudu", help="GitLab group (default: ocudu)")
    parser.add_argument("--ignore", action="append", default=[], metavar="PROJECT[:SCHEDULE]",
                        help=f"Leave out a project's schedules, or one; adds to {IGNORE}")
    parser.add_argument("--base-url", default="https://gitlab.com")
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""),
                        help="GitLab access token (default: $GITLAB_TOKEN)")
    args = parser.parse_args()
    client = Client(args.base_url, args.token)
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    try:
        results = scheduled_pipelines(client, args.group, since, IGNORE + args.ignore)
        failed = [r for r in results if r["pipeline"]["status"] in {"failed"} | RUNNING]
        replies = triage_replies(client, args.group, since - timedelta(days=1),
                                 {r["pipeline"]["id"] for r in failed})
        untriaged = [r for r in failed if r["pipeline"]["id"] not in replies and r["pipeline"]["status"] == "failed"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            flags = pool.map(lambda r: infra_only(client, r["project"]["id"], r["pipeline"]["id"]), untriaged)
            infra = {r["pipeline"]["id"]: flag for r, flag in zip(untriaged, flags)}
        bugs = other_bugs(client, args.group, since)
    except GitLabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(render(results, replies, infra, bugs), end="")


if __name__ == "__main__":
    main()
