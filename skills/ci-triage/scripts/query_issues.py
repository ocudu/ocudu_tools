#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Search the issues of a GitLab group.

Usage:
    query_issues.py --group GROUP [--state S] [--labels L] [--search K] [--with-triage]

Prints {"issues": [...], "total": <count>}. With --with-triage, each issue carries `triage`: the failures earlier
triages matched to it, newest first. For an issue's notes, use get_issue.py.

Authentication: $GITLAB_TOKEN env var or --token.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from gitlab_api import Client, GitLabError, slim_issue, triage_occurrences


def main() -> None:
    parser = argparse.ArgumentParser(description="Search GitLab group issues")
    parser.add_argument("--group", required=True, help="Group path, e.g. ocudu")
    parser.add_argument("--state", default="opened", choices=["opened", "closed", "all"])
    parser.add_argument("--labels", default="", help="Comma-separated labels, e.g. 'bug::ci'")
    parser.add_argument("--search", default="", help="Keyword search in title and description")
    parser.add_argument("--per-page", type=int, default=100,
                        help="Max results for a single-page query (default 100). Ignored when --all-pages is set.")
    parser.add_argument("--all-pages", action="store_true", help="Paginate until all results are fetched.")
    parser.add_argument("--with-triage", action="store_true",
                        help="Add the failures earlier triages matched to each issue (one request per issue).")
    parser.add_argument("--base-url", default="https://gitlab.com",
                        help="GitLab instance URL (default: https://gitlab.com)")
    parser.add_argument("--token", default=os.environ.get("GITLAB_TOKEN", ""),
                        help="Personal access token (default: $GITLAB_TOKEN)")
    args = parser.parse_args()
    client = Client(args.base_url, args.token)

    try:
        path = f"groups/{args.group.replace('/', '%2F')}/issues"
        params: dict = {"state": args.state}
        if args.labels:
            params["labels"] = args.labels
        if args.search:
            params["search"] = args.search
        if args.all_pages:
            raw = client.get_all(path, params)
        else:
            raw = client.get(path, {**params, "per_page": args.per_page})
            raw = raw if isinstance(raw, list) else []

        issues = [slim_issue(issue) for issue in raw]
        if args.with_triage:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for slim, triage in zip(issues, pool.map(lambda i: triage_occurrences(client, i), raw)):
                    slim["triage"] = triage
    except (ValueError, GitLabError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"issues": issues, "total": len(issues)}, indent=2))


if __name__ == "__main__":
    main()
