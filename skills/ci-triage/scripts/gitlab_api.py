# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Minimal GitLab REST and GraphQL client shared by the ci-triage scripts. Standard library only."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

# Hidden markers of the triage thread, its replies, and how a reply carries its failures.
THREAD_MARKER = "<!-- ocudu-ai-triage -->"
_RUN_MARKER_RE = re.compile(r"<!-- ocudu-ai-triage-run (\{.*?\}) -->", re.S)

_ANSI_RE = re.compile(r"(?:\x1b|#x1B)\[[0-9;]*[A-Za-z]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)
TIMEOUT = 60  # seconds without a byte from GitLab before a request fails


class GitLabError(Exception):
    pass


class Client:
    def __init__(self, base_url: str = "https://gitlab.com", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self._headers = {"PRIVATE-TOKEN": token} if token else {}

    def _request(self, method: str, url: str, data: dict | None = None) -> bytes:
        headers = dict(self._headers)
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise GitLabError(f"HTTP {exc.code} {method} {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GitLabError(f"{method} {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitLabError(f"{method} {url}: no response in {TIMEOUT} s") from exc

    def _url(self, path: str, params: dict | None = None) -> str:
        url = f"{self.base_url}/api/v4/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        return url

    def get_raw(self, path: str, params: dict | None = None) -> bytes:
        return self._request("GET", self._url(path, params))

    def get(self, path: str, params: dict | None = None):
        return json.loads(self.get_raw(path, params).decode())

    def get_all(self, path: str, params: dict | None = None, limit: int = 0) -> list:
        """Every page of a list endpoint; stops after `limit` items when given."""
        items, page = [], 1
        while True:
            batch = self.get(path, {**(params or {}), "per_page": 100, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100 or (limit and len(items) >= limit):
                break
            page += 1
        return items[:limit] if limit else items

    def post(self, path: str, data: dict):
        return json.loads(self._request("POST", self._url(path), data).decode() or "null")

    def put(self, path: str, data: dict):
        return json.loads(self._request("PUT", self._url(path), data).decode() or "null")

    def ranged(self, path: str, tail: int):
        """Byte-range access to a download: (fetch_range(start, end_exclusive), size, (start, last `tail` bytes)), or
        None if it doesn't exist. A redirect (to object storage) is followed without the token."""
        url, headers = self._url(path), dict(self._headers)
        try:
            with _NO_REDIRECT.open(urllib.request.Request(url, headers=headers, method="HEAD"), timeout=TIMEOUT):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (301, 302, 303, 307, 308):
                raise GitLabError(f"HTTP {exc.code} HEAD {url}") from exc
            url, headers = exc.headers["Location"], {}
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitLabError(f"HEAD {url}: {exc}") from exc

        def get(range_header: str) -> tuple[bytes, str]:
            req = urllib.request.Request(url, headers={**headers, "Range": range_header})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    if resp.status != 206:
                        raise GitLabError(f"no range support at {url.split('?')[0]}")
                    return resp.read(), resp.headers["Content-Range"]
            except (urllib.error.URLError, TimeoutError) as exc:
                raise GitLabError(f"GET {url.split('?')[0]}: {exc}") from exc

        data, content_range = get(f"bytes=-{tail}")
        size = int(content_range.rsplit("/", 1)[1])
        return (lambda start, end: get(f"bytes={start}-{end - 1}")[0]), size, (size - len(data), data)

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        raw = self._request("POST", f"{self.base_url}/api/graphql", {"query": query, "variables": variables or {}})
        result = json.loads(raw.decode())
        if result.get("errors"):
            raise GitLabError(f"GraphQL: {result['errors']}")
        return result["data"]


def project_path(project: str | int) -> str:
    """`ocudu/ocudu` or a numeric ID as an API path segment."""
    return f"projects/{urllib.parse.quote(str(project), safe='')}"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


_FAILED_CASES_QUERY = """
query($project: ID!, $pipeline: CiPipelineID!, $builds: [ID!]!, $after: String) {
  project(fullPath: $project) {
    pipeline(id: $pipeline) {
      testSuite(buildIds: $builds) {
        testCases(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes { name classname status systemOutput stackTrace recentFailures { count baseBranch } }
        }
      }
    }
  }
}
"""


def failed_test_cases(client: Client, project: str, pipeline_id: int, build_ids: list[int]) -> list[dict]:
    """Failed and errored test cases of the given jobs. GitLab lists them before passed ones."""
    cases, after = [], None
    while True:
        data = client.graphql(_FAILED_CASES_QUERY, {
            "project": project,
            "pipeline": f"gid://gitlab/Ci::Pipeline/{pipeline_id}",
            "builds": [str(b) for b in build_ids],
            "after": after,
        })
        suite = ((data.get("project") or {}).get("pipeline") or {}).get("testSuite")
        if not suite:
            return cases
        page = suite["testCases"]
        for node in page["nodes"]:
            if node["status"] not in ("failed", "error"):
                return cases
            cases.append(node)
        if not page["pageInfo"]["hasNextPage"]:
            return cases
        after = page["pageInfo"]["endCursor"]


def pipeline_tree(client: Client, project: str, pipeline_id: int) -> list[dict]:
    """The pipeline and its downstream pipelines, recursively: [{project, id, parent_job}]."""
    tree = [{"project": project, "id": pipeline_id, "bridge": None}]
    i = 0
    while i < len(tree):
        node = tree[i]
        i += 1
        for bridge in client.get_all(f"{project_path(node['project'])}/pipelines/{node['id']}/bridges"):
            down = bridge.get("downstream_pipeline")
            if not down:
                continue
            m = re.match(r"https?://[^/]+/(.+?)/-/pipelines/\d+", down.get("web_url", ""))
            tree.append({
                "project": m.group(1) if m else down["project_id"],
                "id": down["id"],
                "bridge": bridge.get("name"),
            })
    return tree


def encode_run_marker(data: dict) -> str:
    """A reply's hidden marker; `>` is escaped so the JSON cannot close the HTML comment."""
    return f"<!-- ocudu-ai-triage-run {json.dumps(data, sort_keys=True).replace('>', chr(92) + 'u003e')} -->"


def decode_run_marker(body: str) -> dict | None:
    m = _RUN_MARKER_RE.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def triage_thread(discussions: list[dict]) -> dict | None:
    """The discussion whose first note is the triage thread marker."""
    for d in discussions:
        notes = d.get("notes") or []
        if notes and notes[0].get("body", "").lstrip().startswith(THREAD_MARKER):
            return d
    return None


def triage_runs(discussion: dict | None) -> list[dict]:
    """Decoded run markers of a triage thread, oldest first."""
    if not discussion:
        return []
    runs = []
    for note in discussion.get("notes") or []:
        data = decode_run_marker(note.get("body", ""))
        if data:
            runs.append(data)
    return runs


# Issues

REF_RE = re.compile(r"^(?:https?://[^/]+/)?(.+?)(?:/-/(?:issues|work_items)/|#)(\d+)/?$")
MAX_BODY = 1500
MAX_TRIAGE = 20


def clip(text: str | None, limit: int = MAX_BODY) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def slim_issue(issue: dict) -> dict:
    return {
        "ref": issue["references"]["full"],
        "project_id": issue["project_id"],
        "iid": issue["iid"],
        "title": issue["title"],
        "state": issue["state"],
        "labels": issue.get("labels", []),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "user_notes_count": issue.get("user_notes_count", 0),
        "web_url": issue["web_url"],
        "description": clip(issue.get("description")),
    }


def issue_discussions(client: Client, issue: dict) -> list[dict]:
    if not issue.get("user_notes_count"):
        return []
    return client.get_all(f"projects/{issue['project_id']}/issues/{issue['iid']}/discussions")


def triage_occurrences(client: Client, issue: dict, discussions: list[dict] | None = None) -> list[dict]:
    """Failures matched to the issue by earlier triages, newest first."""
    if discussions is None:
        discussions = issue_discussions(client, issue)
    occurrences = []
    for run in reversed(triage_runs(triage_thread(discussions))):
        for failure in run.get("failures", []):
            occurrences.append({"pipeline": run.get("pipeline"), "date": run.get("date"), **failure})
    return occurrences[:MAX_TRIAGE]


def parse_ref(ref: str) -> tuple[str, int]:
    m = REF_RE.match(ref.strip())
    if not m:
        raise ValueError(f"Not an issue reference: {ref!r} (expected group/project#iid or an issue URL)")
    return m.group(1), int(m.group(2))
