#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Get the details of one or more GitLab issues: the issue, its notes, and the failures earlier triages matched to it.

Usage:
    get_issue.py <group/project#iid | issue URL> [...] [--max-notes N]

Prints {"issues": [...]}, in the order given. Each issue carries `notes` (comments, newest first, without system
notes) and `triage` (newest first), whether it is open or closed.

Authentication: $GITLAB_TOKEN env var or --token.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from gitlab_api import (Client, GitLabError, clip, issue_discussions, parse_ref, project_path, slim_issue,
                        triage_occurrences)


def get_issue(client: Client, ref: str, max_notes: int) -> dict:
    project, iid = parse_ref(ref)
    issue = client.get(f"{project_path(project)}/issues/{iid}")
    discussions = issue_discussions(client, issue)
    notes = [n for d in discussions for n in d.get("notes") or [] if not n.get("system")]
    notes.sort(key=lambda n: n["created_at"], reverse=True)
    return {
        **slim_issue(issue),
        "notes": [{"author": n["author"]["username"], "created_at": n["created_at"], "body": clip(n.get("body"))}
                  for n in notes[:max_notes]],
        "triage": triage_occurrences(client, issue, discussions),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Get GitLab issues with their notes and earlier triages")
    parser.add_argument("refs", nargs="+", metavar="REF", help="group/project#iid or an issue URL")
    parser.add_argument("--max-notes", type=int, default=30, help="Newest notes kept per issue (default 30)")
    parser.add_argument("--base-url", default="https://gitlab.com",
                        help="GitLab instance URL (default: https://gitlab.com)")
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""),
                        help="Personal access token (default: $GITLAB_TOKEN)")
    args = parser.parse_args()
    client = Client(args.base_url, args.token)

    try:
        for ref in args.refs:
            parse_ref(ref)
        with ThreadPoolExecutor(max_workers=8) as pool:
            issues = list(pool.map(lambda r: get_issue(client, r, args.max_notes), args.refs))
    except (ValueError, GitLabError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"issues": issues}, indent=2))


if __name__ == "__main__":
    main()
