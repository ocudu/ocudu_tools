#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Group the failures of a fetched pipeline that are likely one problem, so each group is decided once.

Usage:
    fetch_pipeline.py --quiet <url> | group_failures.py
    group_failures.py <output_dir>

Reads fetch_pipeline.py's JSON from stdin, or <output_dir>/fetch.json. A group is the failures with the same first
error line in their test output (paths, addresses and numbers replaced), in any job; else the same job and test
suite. An E2E (pytest) test is a group of its own: there a test is a scenario, and the same failed criterion in
another scenario may be another problem. So is a job failure, except an infrastructure one, grouped by its reason
and marked `infra`: a job that GitLab says failed for a reason other than its script (the runner, the scheduler, a
timeout), or a test or job whose output has a known cluster error (INFRA_PATTERNS: a pod not created, not enough
resources).

Writes <output_dir>/groups.json, every group with all its failure IDs, and prints a summary: per group, its jobs, a
few tests, and one example failure to read.
"""

import json
import re
import sys
from pathlib import Path

# Error lines, most telling first: the first pattern found in a test's output names its group.
_ERROR_PATTERNS = [re.compile(p) for p in (
    r"runtime error:|ERROR: \w*Sanitizer|SUMMARY: \w*Sanitizer",
    r"^E {2,}\S",                                                    # pytest's error lines, not its source lines
    r"Assertion .* failed|assertion failed|terminate called|Segmentation fault|Aborted|SIG(SEGV|ABRT|BUS|FPE)",
    r"\bFailure\b|didn't pass|Traceback|Exception|\berror:|\bERROR\b|timed out|Timeout",
)]
# GitLab's failure_reason of a job that failed by its own script; any other reason is the infrastructure's.
_SCRIPT_REASONS = {None, "script_failure"}
# Errors of the cluster, not of the code under test, found in a test's output or a job's log: (pattern, reason).
INFRA_PATTERNS = [(re.compile(p), reason) for p, reason in (
    (r"Pod was rejected|Node didn't have enough resource", "k8s: not enough resources"),
    (r"timed out waiting for pod to start|waiting for pods to be ready", "k8s: pod did not start"),
    (r"ImagePullBackOff|ErrImagePull", "k8s: image pull failed"),
    (r"Error creating the pod", "k8s: pod not created"),
    (r"Uploading artifacts for successful job[\s\S]*FATAL: .*artifacts", "runner: artifact upload failed"),
)]
_TRACE_TAIL = 300   # lines of a job's log searched for infra errors
_PYTEST_RE = re.compile(r"^E {2,}\S", re.M)
_MAX_SIGNATURE = 160
_SAMPLE_TESTS = 5
_LIST_FAILURES = 10


def error_signature(text: str) -> str | None:
    """The test's first error line, without paths, addresses and numbers, so that the same error matches."""
    lines = text.splitlines()
    for pattern in _ERROR_PATTERNS:
        for line in lines:
            if pattern.search(line):
                line = re.sub(r"\S*/", "", line)
                line = re.sub(r"0x[0-9a-fA-F]+", "ADDR", line)
                line = re.sub(r"\d+(\.\d+)?(e[+-]?\d+)?", "N", line)
                return re.sub(r"\s+", " ", line).strip()[:_MAX_SIGNATURE]
    return None


def _read(path: str | None, tail: int = 0) -> str:
    if not path:
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-tail:]) if tail else text


def infra_reason(failure: dict, text: str) -> str | None:
    """Why the infrastructure, not the code, failed it: GitLab's failure_reason of a job that failed for anything but
    its script, else a known cluster error in `text`."""
    reason = failure["job"].get("failure_reason")
    if failure["test"] is None and reason not in _SCRIPT_REASONS:
        return reason
    return next((why for pattern, why in INFRA_PATTERNS if pattern.search(text)), None)


def _group_key(failure: dict) -> tuple[tuple, str | None]:
    """(grouping key, error signature)."""
    if failure["test"] is None:
        text = _read(failure.get("trace"), _TRACE_TAIL)
    else:
        text = _read(failure["output"])
    infra = infra_reason(failure, text)
    if infra:
        return ("infra", infra), None
    if failure["test"] is None:
        return ("job", failure["job"]["id"]), None
    signature = error_signature(text)
    if _PYTEST_RE.search(text):
        # E2E: a test is a scenario; the same failed criterion in another scenario may be another problem.
        return ("test", failure["job"]["id"], failure["test"]), signature
    if signature:
        return ("error", signature), signature
    return ("suite", failure["job"]["id"], re.split(r"[/.]", failure["test"])[0]), None


def group_failures(failures: list[dict]) -> list[dict]:
    """Failures that are likely one problem: the same error line in their test output, in any job; else the same job
    and test suite. An E2E (pytest) test and a job failure are groups of their own, unless the infrastructure failed
    the job."""
    groups: dict = {}
    signatures: dict = {}
    for f in failures:
        key, signatures[f["id"]] = _group_key(f)
        groups.setdefault(key, []).append(f)
    out = []
    for n, (key, members) in enumerate(groups.items(), 1):
        gid = f"G{n}"
        jobs = list({f["job"]["id"]: f["job"] for f in members}.values())
        tests = list(dict.fromkeys(f["test"] for f in members if f["test"]))
        out.append({
            "id": gid,
            "signature": signatures[members[0]["id"]],
            "infra": key[1] if key[0] == "infra" else None,
            "count": len(members),
            "jobs": [{k: j.get(k) for k in ("name", "allow_failure", "via")} for j in jobs],
            "tests": tests[:_SAMPLE_TESTS],
            "more_tests": max(0, len(tests) - _SAMPLE_TESTS),
            "failures": [f["id"] for f in members],
            "example": members[0]["id"],
        })
    return out


def summary(fetched: dict, grouped: list[dict]) -> dict:
    """What the model reads: the groups, each with one example failure to open."""
    by_id = {f["id"]: f for f in fetched["failures"]}
    groups = []
    for g in grouped:
        ex = by_id[g["example"]]
        groups.append({
            **{k: g[k] for k in ("id", "signature", "infra", "count", "jobs", "tests", "more_tests")},
            **({"failures": g["failures"]} if g["count"] <= _LIST_FAILURES else {}),
            "example": {k: ex[k] for k in ("id", "output", "trace", "recent_failures")} |
                       {"artifacts": ex["artifacts"][:_SAMPLE_TESTS]},
        })
    return {"pipeline": fetched["pipeline"], "output_dir": fetched["output_dir"], "groups": groups}


def main():
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1].startswith("-")):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) == 2:
        fetched = json.loads((Path(sys.argv[1]) / "fetch.json").read_text(encoding="utf-8"))
    else:
        fetched = json.load(sys.stdin)
    grouped = group_failures(fetched["failures"])
    (Path(fetched["output_dir"]) / "groups.json").write_text(json.dumps({"groups": grouped}, indent=2),
                                                             encoding="utf-8")
    print(json.dumps(summary(fetched, grouped), indent=2))


if __name__ == "__main__":
    main()
