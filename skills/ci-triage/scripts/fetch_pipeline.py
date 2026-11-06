#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Fetch the failures of a GitLab pipeline (and its downstream pipelines).

Usage:
    fetch_pipeline.py <url> [-o OUTPUT_DIR] [--token TOKEN]

URL forms accepted:
  https://gitlab.example.com/group/project/-/jobs/<job_id>
  https://gitlab.example.com/group/project/-/pipelines/<pipeline_id>
  https://gitlab.example.com/api/v4/projects/<project_id>/pipeline_schedules/<schedule_id>

A failure is one failing test of a failed job, read from the pipeline's test report, or the whole job when the report
has no failing test for it (build or setup failures, or no report). Each failure carries its job, the job's trace,
and its artifacts: the test's own when its path names the test, else the job's. A job allowed to fail is triaged like
any other; its `allow_failure` says so.

Output layout:
  <output_dir>/
    <pipeline_id>/
      fetch.json          # what is printed to stdout
      <job_id>/
        trace.txt         # raw job log
        artifacts/...     # known log files from the job's artifacts
        tests/<n>.txt     # output of each failing test, ANSI codes stripped
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import remote_zip
from gitlab_api import Client, GitLabError, failed_test_cases, pipeline_tree, project_path, strip_ansi

# https://gitlab.example.com/group/sub/project/-/jobs/123
# https://gitlab.example.com/group/sub/project/-/pipelines/123
_WEB_URL_RE = re.compile(r"(https?://[^/]+)/(.+?)/-/(jobs|pipelines)/(\d+)/?$")

# https://gitlab.example.com/api/v4/projects/<project_id>/pipeline_schedules/<schedule_id>
_SCHEDULE_API_RE = re.compile(r"(https?://[^/]+)/api/v4/projects/([^/]+)/pipeline_schedules/(\d+)/?$")

# Artifact files to download from each failed job (matched against the full artifact path).
_ARTIFACT_PATTERNS: list[re.Pattern] = [
    re.compile(r"Testing/Temporary/MemoryChecker\.[^/]*\.log$"),
    re.compile(r"agent-log[^/]*\.log$"),
    re.compile(r"[^/]*stdout[^/]*\.log$"),
]


def _pick(data: dict, *keys) -> dict:
    return {k: data[k] for k in keys if k in data}


def _resolve(client: Client, raw_url: str, log) -> tuple[str, int, int | None]:
    """Returns (project, pipeline_id, schedule ID when the URL is a schedule's)."""
    url = raw_url.rstrip("/")

    m = _SCHEDULE_API_RE.match(url)
    if m:
        project, schedule_id = m.group(2), int(m.group(3))
        log(f"Schedule URL — fetching schedule {schedule_id} ...")
        pipelines = client.get(f"{project_path(project)}/pipeline_schedules/{schedule_id}/pipelines",
                               {"per_page": 1, "order_by": "id", "sort": "desc"})
        if not pipelines:
            raise ValueError(f"No pipelines found for schedule {schedule_id}")
        log(f"  latest pipeline: {pipelines[0]['id']}")
        return project, pipelines[0]["id"], schedule_id

    m = _WEB_URL_RE.match(url)
    if not m:
        raise ValueError(f"Cannot parse as GitLab job/pipeline/schedule URL: {raw_url!r}")
    project, kind, id_ = m.group(2), m.group(3), int(m.group(4))
    if kind == "jobs":
        log(f"Job URL — fetching job {id_} to resolve pipeline ...")
        return project, client.get(f"{project_path(project)}/jobs/{id_}")["pipeline"]["id"], None
    return project, id_, None


def _schedule_of(client: Client, project: str, pipeline_id: int, schedule_id: int | None,
                 description: str) -> dict:
    """{id, description} of the schedule that started the pipeline: by ID, by description, else by its pipelines."""
    schedules = client.get(f"{project_path(project)}/pipeline_schedules", {"per_page": 100})
    for sched in schedules:
        if sched["id"] == schedule_id or (description and sched.get("description") == description):
            return {"id": sched["id"], "description": sched.get("description", "")}
    with ThreadPoolExecutor(max_workers=8) as pool:
        recent = pool.map(lambda sc: client.get(f"{project_path(project)}/pipeline_schedules/{sc['id']}/pipelines",
                                                {"per_page": 50, "sort": "desc"}), schedules)
        for sched, pipelines in zip(schedules, recent):
            if any(p["id"] == pipeline_id for p in pipelines):
                return {"id": sched["id"], "description": sched.get("description", "")}
    return {"id": None, "description": description}


def _open_artifacts(client: Client, project: str, job_id: int):
    """Range access to a job's artifacts archive, with its tail already read, or None."""
    return client.ranged(f"{project_path(project)}/jobs/{job_id}/artifacts", remote_zip.TAIL)


def _extract_artifacts(remote, job_dir: Path, tests: list[str]) -> list[str]:
    """Known log files of the archive, read by byte ranges: the failed tests' own when their paths name them, else
    all. A 180 MB E2E archive costs a few MB."""
    if not remote:
        return []

    def select(names: list[str]) -> list[str]:
        logs = [n for n in names if any(p.search(n) for p in _ARTIFACT_PATTERNS)]
        own = [n for n in logs if any(f"/{t}/" in n for t in tests)]
        return own or logs

    paths = []
    for name, content in remote_zip.extract(*remote, select).items():
        dest = job_dir / "artifacts" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        paths.append(str(dest))
    return sorted(paths)


def _artifacts_of(artifacts: list[str], test: str | None) -> list[str]:
    """A test's own artifacts, found by a path segment named after it (Retina: tests/<file>/<test>/...); all the
    job's when none is, or for a job-level failure."""
    if test is None:
        return artifacts
    own = [a for a in artifacts if f"/{test}/" in a]
    return own or artifacts


def _job_info(job: dict, node: dict) -> dict:
    return {**_pick(job, "id", "name", "stage", "web_url"), "allow_failure": bool(job.get("allow_failure")),
            "project": node["project"], "pipeline_id": node["id"], "via": node["bridge"]}


def _fetch_job(client: Client, node: dict, job: dict, has_failed_tests: bool, pipeline_dir: Path, log) -> list[dict]:
    """The failures of one failed job, with its trace, test outputs and artifacts on disk."""
    proj, pid = node["project"], node["id"]
    info = _job_info(job, node)
    job_dir = pipeline_dir / str(job["id"])
    job_dir.mkdir(exist_ok=True)
    log(f"  [{job['id']}] {job['name']} ...")

    def get_trace() -> str | None:
        try:
            text = client.get_raw(f"{project_path(proj)}/jobs/{job['id']}/trace").decode(errors="replace")
        except GitLabError as exc:
            log(f"    trace: skipped ({exc})")
            return None
        (job_dir / "trace.txt").write_text(text, encoding="utf-8")
        return str(job_dir / "trace.txt")

    # The test report is slow to load (seconds for a 12k-test suite): trace and archive index meanwhile.
    with ThreadPoolExecutor(max_workers=2) as pool:
        trace_future = pool.submit(get_trace)
        remote_future = pool.submit(_open_artifacts, client, proj, job["id"])
        cases = failed_test_cases(client, proj, pid, [job["id"]]) if has_failed_tests else []
        artifacts = _extract_artifacts(remote_future.result(), job_dir, [c["name"] for c in cases])
        trace = trace_future.result()

    if not cases:
        return [{"job": info, "test": None, "output": None, "trace": trace, "artifacts": artifacts,
                 "recent_failures": None}]
    (job_dir / "tests").mkdir(exist_ok=True)
    failures = []
    for n, case in enumerate(cases, 1):
        out = job_dir / "tests" / f"{n}.txt"
        text = "\n\n".join(t for t in (case.get("stackTrace"), case.get("systemOutput")) if t)
        out.write_text(strip_ansi(text), encoding="utf-8")
        recent = case.get("recentFailures")
        failures.append({
            "job": info,
            "test": case["name"],
            "output": str(out),
            "trace": trace,
            "artifacts": _artifacts_of(artifacts, case["name"]),
            "recent_failures": {"count": recent["count"], "branch": recent["baseBranch"]} if recent else None,
        })
    return failures


def fetch(client: Client, project: str, pipeline_id: int, schedule_id: int | None, output_dir: Path, log) -> dict:
    root = client.get(f"{project_path(project)}/pipelines/{pipeline_id}")
    if project.isdigit():
        project = client.get(project_path(project))["path_with_namespace"]
    # In a CI job of this same pipeline, GitLab already names the schedule.
    schedule = ""
    if os.environ.get("CI_PIPELINE_ID") == str(pipeline_id):
        schedule = os.environ.get("CI_PIPELINE_SCHEDULE_DESCRIPTION", "")
    sched = {"id": schedule_id, "description": schedule}
    if root.get("source") == "schedule":
        sched = _schedule_of(client, project, pipeline_id, schedule_id, schedule)

    pipeline_dir = output_dir / str(pipeline_id)
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "pipeline": {
            **_pick(root, "id", "ref", "sha", "status", "source", "created_at", "web_url"),
            "project": project,
            "schedule": sched["description"],
            "schedule_id": sched["id"],
        },
        "output_dir": str(pipeline_dir),
        "failures": [],
    }

    for node in pipeline_tree(client, project, pipeline_id):
        proj, pid = node["project"], node["id"]
        failed = client.get_all(f"{project_path(proj)}/pipelines/{pid}/jobs", {"scope[]": "failed"})
        log(f"Pipeline {proj} {pid}: {len(failed)} failed jobs")
        if not failed:
            continue

        # Suites with failing tests, by job: a suite is named after its job and lists the jobs that fed it.
        failing_builds = set()
        for suite in client.get(f"{project_path(proj)}/pipelines/{pid}/test_report_summary")["test_suites"]:
            if suite.get("failed_count") or suite.get("error_count"):
                failing_builds.update(suite.get("build_ids") or [])

        def fetch_one(job: dict) -> list[dict]:
            return _fetch_job(client, node, job, job["id"] in failing_builds, pipeline_dir, log)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for failures in pool.map(fetch_one, failed):
                result["failures"] += failures

    result["failures"] = [{"id": f"F{n}", **f} for n, f in enumerate(result["failures"], 1)]

    (pipeline_dir / "fetch.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch the failures of a GitLab pipeline for triage")
    parser.add_argument("url", help="GitLab job URL, pipeline URL, or schedule API URL")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("/tmp/triage"),
                        help="Root output directory (default: /tmp/triage)")
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""), metavar="TOKEN",
                        help="GitLab personal/project access token (default: $GITLAB_TOKEN)")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress progress output (stderr). JSON result is always printed to stdout.")
    args = parser.parse_args()

    def log(*msg):
        if not args.quiet:
            print(*msg, file=sys.stderr)

    base_url = re.match(r"https?://[^/]+", args.url)
    if not base_url:
        parser.error(f"Cannot extract base URL from: {args.url!r}")
    client = Client(base_url.group(), token=args.token)

    try:
        project, pipeline_id, schedule_id = _resolve(client, args.url, log)
        result = fetch(client, project, pipeline_id, schedule_id, args.output_dir, log)
    except (ValueError, GitLabError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
