---
name: ci-triage
description: >
  Triage failed jobs in a GitLab CI pipeline — match each failing test to one issue in the ocudu group, or flag
  it as new. Use when given a GitLab pipeline, job, or schedule URL. Trigger phrases include: "triage this
  pipeline", "triage this job", "why did this CI job fail", "what failed in this pipeline", "investigate this CI
  failure", "check this run", or any GitLab URL containing /pipelines/ or /-/jobs/ under gitlab.com/ocudu.
argument-hint: <gitlab-pipeline-or-job-url>
arguments: [url]
version: 0.2.0
user-invocable: true
context: inline
license: BSD-3-Clause-Open-MPI
compatibility: Requires python3 (stdlib only) and jq. Set GITLAB_TOKEN to a personal access token with read_api scope for gitlab.com/ocudu.
allowed-tools: Bash(python3 *ci-triage*), Bash(jq *), Read(//tmp/triage/**), Edit(//tmp/triage/**)
---

# CI Job Failure Triage

**Usage:** `/ci-triage <gitlab-job-url-or-pipeline-url>`

Match every failure of a pipeline, child pipelines included, to **exactly one** issue in the `gitlab.com/ocudu`
group, or flag it as new. A failure is one failing test of a failed job; a job whose test report has no failing test
(build, setup) is one failure. Failures that are likely one problem come grouped, and you decide per group. Output is
`decisions.json`, turned by a script into `triage.json` and a table.

This skill only reads GitLab. Root-cause analysis is not its job: say which issue a failure belongs to, not why it
happened.

> **Prerequisites:** `python3` (stdlib only), `jq`. Set `GITLAB_TOKEN` to a token with `read_api` scope.

> **Tool constraint:**
> - **`/tmp/triage/` paths**: use the `Read` and `Write` tools exclusively. Never run any bash command (`cat`, `ls`,
>   `jq`, `grep`, `tail`, or anything else) on paths under `/tmp/triage/` — every new directory triggers a
>   permission prompt.
> - **GitLab data**: only through this skill's scripts; their JSON is the tool result, already in context.
> - **`jq`**: only on piped script output, e.g. `python3 .../query_issues.py ... | jq '.issues[].title'`.
> - Never use `curl`, `wget`, `glab`, or direct HTTP calls. Never use inline `python3 -c`.

---

## Step 1 — Fetch failures and known issues in parallel

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_pipeline.py --quiet "$url" | python3 ${CLAUDE_SKILL_DIR}/scripts/group_failures.py
```

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/query_issues.py --group ocudu --labels "bug::ci" --state opened --with-triage
```

`fetch_pipeline.py` follows downstream pipelines and reads failing tests from the test reports; it saves every
failure, with its job, test output, trace and artifacts, to `<output_dir>/fetch.json`. `group_failures.py` groups the
failures that are likely one problem, saves the groups to `<output_dir>/groups.json`, and prints a summary by group:

```json
{
  "pipeline": {"id": 123, "project": "ocudu/ocudu", "ref": "dev", "schedule": "Nightly", "web_url": "...", ...},
  "output_dir": "/tmp/triage/123",
  "groups": [
    {"id": "G1", "signature": "arm_neon.h:N:N: runtime error: store to misaligned address ADDR for type 'uintN_t' ...",
     "count": 284, "jobs": [{"name": "sanitizers arm neon: [...]", "allow_failure": false, "via": null}, ...],
     "tests": ["ofh_iq_compression_test/...", "..."], "more_tests": 137,
     "example": {"id": "F4", "output": "/tmp/triage/123/456/tests/1.txt", "trace": "/tmp/triage/123/456/trace.txt",
                 "artifacts": ["..."], "recent_failures": {"count": 10, "branch": "dev"}}},
    {"id": "G2", "signature": null, "count": 1, "jobs": [{"name": "build", "via": "trigger deploy", ...}],
     "tests": [], "more_tests": 0, "failures": ["F2"],
     "example": {"id": "F2", "output": null, "trace": "/tmp/triage/124/457/trace.txt", ...}}
  ]
}
```

- A group is the failures with the same first error line in their test output (`signature`: paths, addresses and
  numbers replaced), in any job; failures without a recognised error line are grouped by job and test suite.
- An E2E (pytest) test is a group of its own: a test is a scenario, and the same failed criterion in another
  scenario may be another problem. Give several such groups the same issue when they are one.
- A failed job with no failing test in its report (build, setup) is a group of its own, with `example.output: null`.
- `infra` is set when the runner or the cluster failed it, not the code: GitLab failed the job for a reason other
  than its script (`runner_system_failure`, `stuck_or_timeout_failure`...), or the output has a known cluster error
  (`k8s: not enough resources`, `k8s: pod not created`...). Such groups are decided `Infra` automatically and never
  matched to issues; skip them, or only give a better `key_error` (Step 4). If another group is plainly a cluster
  problem too, decide it normally and say so in `reason`: the pattern list is where to add it.
- `failures` lists the group's failure IDs when there are 10 or fewer; `groups.json` has them all, and `fetch.json`
  every failure's detail. You rarely need either.
- `job.via` names the trigger job of a downstream pipeline. A job allowed to fail is triaged like any other.
- `artifacts` are the test's own logs when their path names the test (E2E), else all of the job's.
- `recent_failures` is GitLab's count of the test's failures on the default branch in the last 14 days, all pipelines
  mixed: a hint that the failure is known, not proof.

If `groups` is empty, report "No failed jobs in this pipeline" and stop.

The issues query returns `{"issues": [...], "total": N}`. Each issue has `ref` (`ocudu/ocudu#990`), `title`, `state`,
`description` (clipped) and `triage`: the failures earlier triages matched to it (`job`, `test`, `key_error`,
`pipeline`, `date`). Keep it in context; it is reused for every failure.

## Step 2 — Read each group

`Read` the group's `example.output`: one test's own output, from the test report. For a job failure
(`output: null`), `Read` its `trace`: start with the last 200 lines (`offset=-200`), and ignore everything after a
line containing `section_start` and `after_script` (cleanup noise). Read more around error markers only if needed.

**Formats:**

- **make** — `error:` lines with file/line, or `ld:` errors
- **ctest / GoogleTest** — `[  FAILED  ] Suite.Test`, `EXPECT_*/ASSERT_*` lines, sanitizer `runtime error:` lines,
  exception messages
- **pytest (Retina E2E)** — `E   Failed: ...` lines, the failed criteria ("Test didn't pass the following criteria:
  X"), `[WARNING]`/`[ERROR]` lines of the per-component log sections

**Extras:**

- **ctest** — a `MemoryChecker.<N>.log` in the failure's `artifacts` holds the Valgrind trace of the N-th test run.
- **E2E** — see [REFERENCE.md](REFERENCE.md) for the Retina artifact layout and test suite structure. The suite
  param (`singleue.bands.cots_band_78_tcp_uplink`) is the primary search key.
- **"Emergency flush of the logger"** is never the root cause — the gNB crashed upstream. Read `agent-log*.log`
  artifacts first, then `*stdout*.log`.

For each group, extract:

1. **key_error** — the most specific diagnostic line (assertion message, sanitizer error, compiler error). For an
   E2E failed criterion, name the symptom too: the criterion says what was measured, the first `[ERROR]`/`[WARNING]`
   of the gNB or UE says what happened (`Error Indications; RLF detected: MAC max consecutive CRC KOs reached`).
2. **error_type** — one of `build | crash | assertion | timeout | setup | criteria`.
3. **excerpt** — up to 30 lines around the key error, for a new issue's logs section.

If the example shows the group mixes different problems (a signature too generic, or no signature), read a few
more of its failures from `fetch.json` and decide those apart (Step 4).

## Step 3 — Match each group to one issue

### 3a — Candidates

1. **Earlier occurrences** — an issue whose `triage` already lists the same job, test and key error (volatile parts
   aside: addresses, IDs, timestamps, slot numbers) is a High candidate.
2. **Open `bug::ci` issues** (in context, no command) — High: suite or test name, or the exact error phrase, in the
   **title**; Medium: key error phrase or component in the **description**. Prefer more recent `updated_at`.
3. **All open issues**, any label — only if no High candidate yet:

   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/query_issues.py --group ocudu --state opened --search "KEYWORD" --per-page 30
   ```

4. **Closed `bug::ci` issues** — only if still nothing:

   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/query_issues.py --group ocudu --labels "bug::ci" --state closed --search "KEYWORD" --per-page 30
   ```

Keywords (2–4 per group), one word or a short phrase each: GitLab matches whole words in titles and descriptions, so
`coverage.uplink` finds nothing where `coverage` finds "RLFs in coverage test". Use words of the suite param
(`coverage`, `inter_du_ho`, `bands`), of the symptom (`RLF`, `CRC KOs`), component labels (`CU-CP`, `open5gs`) and
the failed criterion (`Error Indications`).

### 3b — Read the notes of the top candidates

For the top 3 candidates of each group, High ones included, get the issues with their comments, in one call:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/get_issue.py ocudu/ocudu#990 ocudu/ocudu#985 ocudu/ocudu#979
```

It prints `{"issues": [...]}`, each issue with its `notes` (newest first) and `triage` (earlier occurrences, also for
closed issues).

Notes hold root causes, "same as #123", "not this one, see #456", job links, and human replies to earlier triages in
the issue's `AI triage` thread. Use them to confirm, re-rank or rule out candidates. A human saying an earlier match
was wrong overrides the earlier occurrence. Get each issue once, however many failures it is a candidate for: gather
the top candidates of all failures and make one call.

### 3c — Decide

- **One issue per failure.** When several candidates are plausible (a test that fails for several known reasons),
  pick the most likely one and list the others as `alternatives`. Never more than one issue for one failure.
- **A different key error is a different issue.** Same test, but a key error that doesn't match what the candidate
  describes or what its earlier occurrences show: `No match`, with `related` set to that candidate.
- **A test's history is not evidence by itself.** Earlier occurrences of the same test count only if their key error
  matches this one; a test that failed for this issue before can fail for another reason today.
- **Verdicts:**
  - `Matched` — you are confident it is this issue.
  - `Check` — plausible but uncertain: the best of the candidates; say why in `reason` and give `alternatives`.
  - `No match` — nothing plausible. A new issue is opened for it; failures that belong in the same new issue (same
    job and key error) share a `new_issue` key.
- Closed issues are valid picks; reopening or opening a related issue is decided by the posting script, not here.

## Step 4 — Write `decisions.json`

`Write` `<output_dir>/decisions.json`, one entry per group. An entry for a failure ID overrides its group's decision
for that failure only: use it to split a group.

```json
{
  "decisions": {
    "G1": {
      "error_type": "crash",
      "key_error": "runtime error: reference binding to null pointer of type 'struct mac_pcap'",
      "excerpt": "...",
      "verdict": "Matched",
      "issue": "ocudu/ocudu#962",
      "reason": "same test and UBSan error as the title"
    },
    "G2": {
      "error_type": "criteria", "key_error": "...", "excerpt": "...",
      "verdict": "Check", "issue": "ocudu/ocudu#979", "reason": "...",
      "alternatives": [{"issue": "ocudu/ocudu#985", "reason": "..."}]
    },
    "G3": {
      "error_type": "build", "key_error": "...", "excerpt": "...",
      "verdict": "No match", "new_issue": "A", "related": "ocudu/ocudu#900",
      "title": "fapi_adaptor build fails: undefined slot_point"
    }
  }
}
```

A `No match` needs a `title` for the new issue, up to 80 characters: the problem in a few
words, not where it showed up, since other jobs and tests may hit it too. Name a component or scenario only when it
is part of the problem; no job or test names, numbers, paths or log details: `UE->5GC ping fails, 5GC->UE fine`,
`SMF NGAP error on PDU session setup`. The test is appended when it fits (`… - ping.drx`). Decisions sharing a
`new_issue` use the first one's.

For an `infra` group, an entry is optional. `{"verdict": "Infra", "key_error": "timed out waiting for pod to start"}`
replaces the default key error (`infra: <reason>`). No other verdict is accepted for it, and `Infra` for no
other group.

`issue`, `alternatives[].issue` and `related` are full references (`group/project#iid`). Do not copy URLs or IDs: the script takes them from `fetch.json`.

## Step 5 — Build the table

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/build_triage.py <output_dir>
```

It checks `decisions.json` against `fetch.json`, writes `triage.json` and `triage.md`, and prints the table: one row
per decision (a group, or a failure decided apart), with its jobs, tests, key failure, issue, verdict and recent
failures. If it lists problems, fix `decisions.json` and
run it again.

Show the table as the result. Nothing is written to GitLab.
