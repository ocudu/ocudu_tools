#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Exhaustive mode of the guidelines review: a plan of small work items for subagents, and the checks that nothing was
left out.

    exhaustive.py plan   <results-dir>
    exhaustive.py review <results-dir> [--item <id>]
    exhaustive.py fixes  <results-dir> [--batch <id>]
    exhaustive.py recheck <results-dir>

plan    Splits the code guide into sections (one per heading at a guide file's section level) and the scope into
        batches of a few files (a header and its source stay together), packs each guide file's sections into groups
        of at most REVIEW_GUIDE_WORDS words, and writes one work item per (section group, batch): the brief a reviewer
        subagent follows, with the guide text verbatim, in <results-dir>/exhaustive/review/<item>.md. Its ids are in
        exhaustive/items.txt. Fails when there would be more than REVIEW_MAX_ITEMS items.
review  Checks the reviewers' ledgers (review/<item>.json): every (section, file) pair of the item is there, says
        concretely what was examined, and every finding is well formed. With --item, checks that item only. Without
        it, once every item passes, numbers the findings (findings.json) and writes the brief of a fixer subagent for
        each batch with findings (fix/<batch>.md).
fixes   Checks the fixers' results (fix/<batch>.json): every finding of the batch has a status. With --batch, checks
        that batch only. Without it, once every batch passes, writes the report's guide rows (report-rows.md), its
        findings (findings.md) and the edits fixers left to the main agent (outside-edits.md).

recheck Reviews what the fixers (and the main agent) wrote: plans a second, diff-scope review, in <results-dir>/recheck,
        of every C/C++ line changed since the baseline, against every guide section. A fix can break a rule other than
        the one it fixes, and nothing else checks that. Run `review`, `fixes` on <results-dir>/recheck as on the main
        directory (its findings are in recheck/exhaustive/findings.md); repeat until it has no findings.

Guide files whose name contains "commit" are not planned: commit-message rules are about commits, not files, and the
main agent checks them.

Sizes, from the environment: REVIEW_BATCH_FILES (default 4) and REVIEW_BATCH_LINES (default 1200; changed lines in
diff scope, file lines otherwise) per batch, REVIEW_GUIDE_WORDS (default 1500) per section group, REVIEW_MAX_ITEMS
(default 200).

Exit status: 0 when the check passes, 1 when it does not (the reasons are printed), 2 on bad arguments or setup.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve()

# Guide files the main agent handles itself.
MAIN_AGENT_GUIDE_RE = re.compile(r"commit", re.IGNORECASE)

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")

# "examined" texts that say nothing about what was examined.
VAGUE = {"clean", "ok", "n/a", "na", "none", "no issues", "no issue", "nothing", "no violations", "no violation",
         "not applicable", "fine", "checked", "nothing found", "no findings", "all good", "compliant"}
MIN_EXAMINED = 25
MIN_NOTE = 15
STATUSES = ("fixed", "report-only", "rejected")

# Guide sections every fixer gets, whether or not a finding of its batch is about them: the rules that code added by a
# fix most often breaks. REVIEW_FIX_ALWAYS (headings separated by "|") overrides; matched by prefix, case-insensitive.
DEFAULT_FIX_ALWAYS = ("Const Correctness|Naming Conventions|Function signatures|Passing input parameters|Magic numbers|"
                      "Use `[[nodiscard]]`")


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def load_review(results_dir):
    """The KEY=value pairs of <results-dir>/review.env."""
    env = results_dir / "review.env"
    if not env.is_file():
        die(f"{env} not found; run setup.sh first")
    values = {}
    for line in env.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    return values


def env_int(name, default):
    value = os.environ.get(name, "")
    try:
        n = int(value) if value else default
    except ValueError:
        die(f"{name}={value} is not a number")
    if n < 1:
        die(f"{name} must be at least 1")
    return n


def read_json(path):
    """(data, None), or (None, why it could not be read)."""
    try:
        return json.loads(path.read_text()), None
    except OSError as e:
        return None, str(e)
    except json.JSONDecodeError as e:
        return None, f"not valid JSON: {e}"


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def is_text(value, min_len):
    return isinstance(value, str) and len(value.strip()) >= min_len


def is_vague(text):
    return re.sub(r"[^a-z/ ]", "", text.lower()).strip() in VAGUE


# ---------------------------------------------------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------------------------------------------------


def guide_files(guide):
    """The numbered .md files of the code guide, in their number's order."""

    def key(p):
        return int(re.match(r"\d+", p.name).group()), p.name

    return sorted((p for p in guide.glob("*.md") if re.match(r"\d", p.name)), key=key)


def split_sections(path):
    """(intro, [(heading, body)]) of a guide file. The sections are the headings at the file's section level: the
    level below its title when it has a single top heading, else the top level. The intro is the text before the first
    section; it applies to every section."""
    lines = path.read_text().splitlines()
    heads = []
    fence = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if m:
            c = m.group(1)[0]
            if fence is None:
                fence = c
            elif fence == c:
                fence = None
            continue
        if fence is None:
            h = HEADING_RE.match(line)
            if h:
                heads.append((i, len(h.group(1)), h.group(2)))
    if not heads:
        return "", [("whole file", "\n".join(lines).strip())]
    top = min(level for _, level, _ in heads)
    deeper = [level for _, level, _ in heads if level > top]
    n_top = sum(1 for _, level, _ in heads if level == top)
    level = min(deeper) if n_top == 1 and deeper else top
    starts = [(i, title) for i, lv, title in heads if lv == level]
    intro = "\n".join(lines[:starts[0][0]]).strip()
    sections = []
    for i, title in starts:
        end = next((j for j, lv, _ in heads if j > i and lv <= level), len(lines))
        sections.append((title, "\n".join(lines[i:end]).strip()))
    return intro, sections


def pack_sections(sections, max_words):
    """Consecutive sections of one guide file, grouped into lists of at most max_words words (a larger section alone)."""
    packs, cur, words = [], [], 0
    for s in sections:
        w = len(s["body"].split())
        if cur and words + w > max_words:
            packs.append(cur)
            cur, words = [], 0
        cur.append(s)
        words += w
    if cur:
        packs.append(cur)
    return packs


def file_weights(review, files):
    """Per scoped file, its size for batching: changed lines in diff scope, file lines otherwise."""
    repo = Path(review["REPO"])
    if review.get("MODE") == "diff":
        # DIFF_FROM (recheck): the working tree against that commit; otherwise the review's own range.
        rng = [review["DIFF_FROM"]] if review.get("DIFF_FROM") else [review["MERGE_BASE"], review["BASELINE"]]
        out = subprocess.run(["git", "-C", str(repo), "diff", "--numstat", "--no-renames", *rng],
                             capture_output=True, text=True, check=True).stdout
        changed = {}
        for line in out.splitlines():
            added, removed, path = line.split("\t", 2)
            changed[path] = (int(added) if added.isdigit() else 1) + (int(removed) if removed.isdigit() else 0)
        for f in filter(None, review.get("NEW_FILES", "").split(",")):
            try:
                with open(repo / f, errors="replace") as fh:
                    changed[f] = sum(1 for _ in fh)
            except OSError:
                pass
        return {f: max(1, changed.get(f, 1)) for f in files}
    weights = {}
    for f in files:
        try:
            with open(repo / f, errors="replace") as fh:
                weights[f] = max(1, sum(1 for _ in fh))
        except OSError:
            weights[f] = 1
    return weights


def batch_files(files, weights, max_files, max_lines):
    """The scoped files in batches. Files with the same stem in a directory of the same name (include/ocudu/x/foo.h,
    lib/x/foo.cpp) stay together: a fix to a header usually needs its source."""
    groups = {}
    for f in files:
        p = Path(f)
        groups.setdefault((p.parent.name, p.stem), []).append(f)
    batches, cur, cur_lines = [], [], 0
    for group in groups.values():
        lines = sum(weights[f] for f in group)
        if cur and (len(cur) + len(group) > max_files or cur_lines + lines > max_lines):
            batches.append(cur)
            cur, cur_lines = [], 0
        cur += group
        cur_lines += lines
    if cur:
        batches.append(cur)
    return batches


def scope_rule(review):
    if review.get("MODE") == "diff":
        return ("- Scope: the diff below. Apply the rules to the entities (declarations, functions, classes, statements) "
                "the diff added or changed, and leave untouched code alone, even when it breaks a rule.")
    return "- Scope: whole files. Apply the rules to every line of each file."


def guide_text(sections):
    """The verbatim text of the sections, each guide file's introduction once before its first section."""
    out = []
    shown_intro = set()
    for s in sections:
        if s["intro"] and s["guide"] not in shown_intro:
            shown_intro.add(s["guide"])
            out += [f'<guide-intro file="{s["guide"]}" note="applies to every section of this file">', s["intro"],
                    "</guide-intro>", ""]
        out += [f'<guide-section id="{s["id"]}" file="{s["guide"]}" heading="{s["heading"]}">', s["body"],
                "</guide-section>", ""]
    return out


def review_brief(d, review, iid, files, sections):
    ledger = d / "exhaustive" / "review" / f"{iid}.json"
    ids = ", ".join(s["id"] for s in sections)
    lines = [
        f"# Guidelines review work item {iid}",
        "",
        f"You are one reviewer of an exhaustive OCUDU code-guide review. Your share is {len(sections)} code-guide "
        f"section(s) ({ids}) and {len(files)} file(s): check every rule of every section against every file. Other "
        "reviewers cover the other sections and files.",
        "",
        "## Rules",
        "",
        f"- Do not edit any file in the checkout. The only file you write is your ledger, `{ledger}`.",
        scope_rule(review),
        "- Go through the pairs one at a time: for each section, for each file, read the code against each rule of "
        "the section. Do not call a file clean from a glance.",
        "- Report every violation where it occurs: one finding per place, even when the same problem repeats many "
        "times.",
        "- A rule applies whatever the code's origin, volume or risk: no exemptions for generated-looking code, ASN.1 "
        "types, converters, or files with many occurrences. Report the violation; the fixer decides what can be fixed.",
        "- `examined` says concretely what you checked for that pair: the functions, classes, identifiers, includes or "
        "statements you went through. \"clean\" or \"no issues\" alone is rejected.",
        "- When a section cannot apply to a file, `examined` says which property of the code makes it impossible "
        "(e.g. \"no class is declared in this file\"). A bare \"n/a\" is rejected.",
        "- Line numbers are those of the file as it is now in the checkout.",
        "- `fix` is the change you propose, precise enough for someone else to make it.",
        "",
        "## Files",
        "",
        *[f"- `{f}`" for f in files],
        "",
    ]
    if review.get("MODE") == "diff":
        if review.get("DIFF_FROM"):
            new_files = set(review.get("NEW_FILES", "").split(","))
            new = [f for f in files if f in new_files]
            old = [f for f in files if f not in new_files]
            cmds = [f"git -C {review['REPO']} diff {review['DIFF_FROM']} -- {' '.join(old)}"] if old else []
            cmds += [f"git -C {review['REPO']} diff --no-index -- /dev/null {f}   # new file: every line is under review"
                     for f in new]
        else:
            cmds = [f"git -C {review['REPO']} diff {review['MERGE_BASE']} {review['BASELINE']} -- {' '.join(files)}"]
        lines += ["The diff under review (read the whole files too, for context):", "", "```sh", *cmds, "```", ""]
    lines += ["## Code guide sections", "", "Verbatim from the code guide; they are the only rules of this item.", ""]
    lines += guide_text(sections)
    example = {
        "item": iid,
        "checks": [{
            "section": sections[0]["id"],
            "file": files[0],
            "examined": "<what you went through, e.g. the 6 functions (a, b, ...) and their 31 local variables>",
            "findings": [{"line": 42, "problem": "<what breaks which rule>", "fix": "<the change to make>"}],
        }],
    }
    lines += [
        "## Ledger",
        "",
        f"Write `{ledger}` as JSON, with exactly one entry in `checks` for each of the {len(sections) * len(files)} "
        f"(section, file) pairs (sections {ids} × the files above). `findings` is `[]` for a clean pair.",
        "",
        "```json",
        json.dumps(example, indent=2),
        "```",
        "",
        "Then check it, and fix the ledger until it prints `ok`:",
        "",
        "```sh",
        f"python3 {SCRIPT} review {d} --item {iid}",
        "```",
        "",
        "Reply with only the line it printed.",
    ]
    return "\n".join(lines) + "\n"


def cmd_plan(d, _args):
    review = load_review(d)
    ex = d / "exhaustive"
    if ex.exists():
        die(f"{ex} already exists; remove it to plan again")
    scope = d / "scope.txt"
    files = [l.strip() for l in scope.read_text().splitlines() if l.strip()] if scope.is_file() else []
    if not files:
        die(f"{scope} is missing or empty; run setup.sh first")

    guide = Path(review["GUIDE"])
    max_words = env_int("REVIEW_GUIDE_WORDS", 1500)
    sections, packs, main_agent_guides = [], [], []
    for gpath in guide_files(guide):
        if MAIN_AGENT_GUIDE_RE.search(gpath.stem):
            main_agent_guides.append(gpath.stem)
            continue
        intro, parsed = split_sections(gpath)
        of_file = []
        for heading, body in parsed:
            s = {"id": f"S{len(sections) + 1:02d}", "guide": gpath.stem, "heading": heading, "intro": intro,
                 "body": body}
            sections.append(s)
            of_file.append(s)
        packs += pack_sections(of_file, max_words)
    if not sections:
        die(f"no numbered guide files with sections in {guide}")

    weights = file_weights(review, files)
    batches = batch_files(files, weights, env_int("REVIEW_BATCH_FILES", 4), env_int("REVIEW_BATCH_LINES", 1200))
    n_items = len(packs) * len(batches)
    max_items = env_int("REVIEW_MAX_ITEMS", 200)
    if n_items > max_items:
        die(f"{n_items} work items ({len(packs)} section groups x {len(batches)} file batches) is over "
            f"REVIEW_MAX_ITEMS={max_items}. Narrow the scope, or raise REVIEW_MAX_ITEMS, REVIEW_BATCH_FILES, "
            "REVIEW_BATCH_LINES or REVIEW_GUIDE_WORDS")

    bw = max(2, len(str(len(batches))))
    pw = max(2, len(str(len(packs))))
    batch_ids = {f"b{n:0{bw}d}": b for n, b in enumerate(batches, 1)}
    by_id = {s["id"]: s for s in sections}
    items = {}
    for bid in batch_ids:
        for p, pack in enumerate(packs, 1):
            items[f"{bid}-p{p:0{pw}d}"] = {"batch": bid, "sections": [s["id"] for s in pack]}

    (ex / "review").mkdir(parents=True)
    write_json(ex / "plan.json", {"sections": sections, "batches": batch_ids, "items": items,
                                  "main_agent_guides": main_agent_guides})
    for iid, item in items.items():
        brief = review_brief(d, review, iid, batch_ids[item["batch"]], [by_id[s] for s in item["sections"]])
        (ex / "review" / f"{iid}.md").write_text(brief)
    (ex / "items.txt").write_text("".join(f"{iid}\n" for iid in items))

    print(f"{n_items} work items: {len(packs)} groups of {len(sections)} guide sections x {len(batches)} batches of "
          f"{len(files)} files")
    if main_agent_guides:
        print(f"not planned, for the main agent: {', '.join(main_agent_guides)}")
    print(f"briefs: {ex}/review/<item>.md")
    print(f"items:  {ex}/items.txt")
    return 0


# ---------------------------------------------------------------------------------------------------------------------
# Review check
# ---------------------------------------------------------------------------------------------------------------------


def load_plan(d):
    data, err = read_json(d / "exhaustive" / "plan.json")
    if err:
        die(f"{d}/exhaustive/plan.json: {err}; run `exhaustive.py plan` first")
    return data


def check_ledger(d, plan, iid):
    """(errors, findings) of an item's ledger."""
    item = plan["items"][iid]
    path = d / "exhaustive" / "review" / f"{iid}.json"
    if not path.is_file():
        return [f"no ledger at {path}"], []
    data, err = read_json(path)
    if err:
        return [err], []
    if not isinstance(data, dict):
        return ["the ledger must be a JSON object"], []
    errors = []
    if data.get("item") != iid:
        errors.append(f'"item" must be "{iid}"')
    checks = data.get("checks")
    if not isinstance(checks, list):
        return errors + ['"checks" must be a list'], []

    files = plan["batches"][item["batch"]]
    expected = {(s, f) for s in item["sections"] for f in files}
    seen = set()
    findings = []
    for n, c in enumerate(checks):
        where = f"checks[{n}]"
        if not isinstance(c, dict):
            errors.append(f"{where}: not an object")
            continue
        key = (c.get("section"), c.get("file"))
        if key not in expected:
            errors.append(f"{where}: ({key[0]}, {key[1]}) is not a pair of this item")
            continue
        if key in seen:
            errors.append(f"{where}: ({key[0]}, {key[1]}) appears twice")
            continue
        seen.add(key)
        where = f"({key[0]}, {key[1]})"
        examined = c.get("examined")
        if not is_text(examined, MIN_EXAMINED) or is_vague(examined):
            errors.append(f'{where}: "examined" must say concretely what was checked (at least {MIN_EXAMINED} '
                          "characters)")
        found = c.get("findings")
        if not isinstance(found, list):
            errors.append(f'{where}: "findings" must be a list ([] when clean)')
            continue
        for m, f in enumerate(found):
            fw = f"{where} findings[{m}]"
            if not isinstance(f, dict):
                errors.append(f"{fw}: not an object")
                continue
            line = f.get("line")
            if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                errors.append(f'{fw}: "line" must be a line number')
            for k in ("problem", "fix"):
                if not is_text(f.get(k), 5):
                    errors.append(f'{fw}: "{k}" must be a sentence')
            findings.append({"item": iid, "batch": item["batch"], "section": key[0], "file": key[1], "line": line,
                             "problem": f.get("problem"), "fix": f.get("fix")})
    for s, f in sorted(expected - seen):
        errors.append(f"missing the pair ({s}, {f})")
    return errors, findings


def fix_brief(d, review, plan, bid, findings):
    result = d / "exhaustive" / "fix" / f"{bid}.json"
    files = plan["batches"][bid]
    by_id = {s["id"]: s for s in plan["sections"]}
    lines = [
        f"# Guidelines review fixes, batch {bid}",
        "",
        f"You fix the {len(findings)} finding(s) below, which reviewers found against the OCUDU code guide. The rules "
        "are the guide sections quoted after them.",
        "",
        "## Rules",
        "",
        "- Edit only these files, in the checkout: " + ", ".join(f"`{f}`" for f in files) + ". Other fixers edit "
        "other files at the same time. When a fix needs a change in another file (a caller, a test, the source that "
        "implements a header), make your part and list the other file in `outside_edits` with what it needs; the main "
        "agent makes those edits afterwards.",
        "- Do not build or run tests: the main agent validates all fixes together afterwards.",
        scope_rule(review),
        "- Line numbers are from before any edit; once you have edited a file, find the place by its content.",
        "- Several findings may touch the same code: make the fixes fit together. When you merge the proposed fixes "
        "of several findings, take each literally: do not add a qualifier, keyword or type that none of them "
        "contains, and keep out what one of them excludes (e.g. a \"const-free\" local stays without `const`).",
        "- Code you write or change is itself subject to every rule of the guide sections below, not only to the one "
        "that made you touch it. Before you finish, read every line you added against those sections.",
        "- When a fix needs a choice (a name, how to split parameters, a doc sentence), follow the conventions of the "
        "surrounding module and record the choice in `choice`.",
        "- Every finding gets a status:",
        "  - `fixed`: done in the file.",
        "  - `report-only`: only when the fix would need a change in a generated file, a bundled or external library, "
        "or another repository; `note` says which. This list is closed: not by volume, risk or origin of the code.",
        "  - `rejected`: the code does not break the rule; `note` says why, quoting the rule.",
        "",
        "## Findings",
        "",
    ]
    for f in findings:
        s = by_id[f["section"]]
        lines += [f"- **{f['id']}** {s['id']} ({s['guide']} § {s['heading']}) — `{f['file']}:{f['line']}` — "
                  f"{f['problem']}", f"  Proposed fix: {f['fix']}"]
    always = [h.strip().lower() for h in os.environ.get("REVIEW_FIX_ALWAYS", DEFAULT_FIX_ALWAYS).split("|")
              if h.strip()]
    wanted = {f["section"] for f in findings}
    wanted |= {s["id"] for s in plan["sections"] if any(s["heading"].lower().startswith(h) for h in always)}
    lines += ["", "## Code guide sections", "",
              "The first sections are those of the findings; the rest are rules that code added by a fix often "
              "breaks. All of them apply to every line you write.", ""]
    lines += guide_text([by_id[sid] for sid in sorted(wanted, key=lambda i: int(i[1:]))])
    example = {
        "batch": bid,
        "results": [{"id": findings[0]["id"], "status": "fixed", "note": "", "choice": "<a choice made, or empty>"}],
        "outside_edits": [{"path": "<another file>", "why": "<what it needs and which finding requires it>"}],
    }
    lines += [
        "## Result",
        "",
        f"Write `{result}` as JSON, with one entry in `results` for each finding above (`outside_edits` is `[]` when "
        "there are none):",
        "",
        "```json",
        json.dumps(example, indent=2),
        "```",
        "",
        "Then check it, and fix it until it prints `ok`:",
        "",
        "```sh",
        f"python3 {SCRIPT} fixes {d} --batch {bid}",
        "```",
        "",
        "Reply with only the line it printed.",
    ]
    return "\n".join(lines) + "\n"


def print_failures(bad, total, what, rerun):
    for key, errors in bad.items():
        print(f"{key}: FAIL")
        for e in errors[:20]:
            print(f"  - {e}")
        if len(errors) > 20:
            print(f"  - ... and {len(errors) - 20} more")
    print(f"\n{total - len(bad)} of {total} {what} pass; {rerun}: {' '.join(bad)}")


def cmd_review(d, args):
    plan = load_plan(d)
    if args.item and args.item not in plan["items"]:
        die(f"unknown item {args.item}")
    ids = [args.item] if args.item else list(plan["items"])
    bad, findings = {}, []
    for iid in ids:
        errors, found = check_ledger(d, plan, iid)
        if errors:
            bad[iid] = errors
        findings += found
    if bad:
        print_failures(bad, len(ids), "items", "start a new reviewer for")
        return 1
    if args.item:
        print(f"{args.item}: ok, {len(findings)} findings")
        return 0

    order = {s["id"]: n for n, s in enumerate(plan["sections"])}
    findings.sort(key=lambda f: (f["batch"], f["file"], f["line"], order[f["section"]]))
    for n, f in enumerate(findings, 1):
        f["id"] = f"F{n:04d}"
    ex = d / "exhaustive"
    write_json(ex / "findings.json", findings)
    by_batch = {}
    for f in findings:
        by_batch.setdefault(f["batch"], []).append(f)
    review = load_review(d)
    (ex / "fix").mkdir(exist_ok=True)
    for bid, found in by_batch.items():
        (ex / "fix" / f"{bid}.md").write_text(fix_brief(d, review, plan, bid, found))
    (ex / "fix-batches.txt").write_text("".join(f"{bid}\n" for bid in by_batch))

    print(f"all {len(ids)} items pass: {len(findings)} findings in {len(by_batch)} of {len(plan['batches'])} batches")
    if by_batch:
        print(f"fixer briefs: {ex}/fix/<batch>.md")
        print(f"batches:      {ex}/fix-batches.txt")
    return 0


# ---------------------------------------------------------------------------------------------------------------------
# Fixes check
# ---------------------------------------------------------------------------------------------------------------------


def check_fix(d, bid, findings):
    """(errors, {finding id: result}, outside edits) of a batch's fix result."""
    path = d / "exhaustive" / "fix" / f"{bid}.json"
    if not path.is_file():
        return [f"no result at {path}"], {}, []
    data, err = read_json(path)
    if err:
        return [err], {}, []
    if not isinstance(data, dict):
        return ["the result must be a JSON object"], {}, []
    errors = []
    if data.get("batch") != bid:
        errors.append(f'"batch" must be "{bid}"')
    results = data.get("results")
    if not isinstance(results, list):
        return errors + ['"results" must be a list'], {}, []
    expected = {f["id"] for f in findings}
    seen = {}
    for n, r in enumerate(results):
        where = f"results[{n}]"
        if not isinstance(r, dict):
            errors.append(f"{where}: not an object")
            continue
        fid = r.get("id")
        if fid not in expected:
            errors.append(f"{where}: {fid} is not a finding of this batch")
            continue
        if fid in seen:
            errors.append(f"{where}: {fid} appears twice")
            continue
        seen[fid] = r
        status = r.get("status")
        if status not in STATUSES:
            errors.append(f'{fid}: "status" must be one of {", ".join(STATUSES)}')
        elif status != "fixed" and not is_text(r.get("note"), MIN_NOTE):
            errors.append(f'{fid}: a {status} finding needs a "note" saying why')
        for k in ("note", "choice"):
            if r.get(k) is not None and not isinstance(r.get(k), str):
                errors.append(f'{fid}: "{k}" must be a string')
    for fid in sorted(expected - set(seen)):
        errors.append(f"missing a result for {fid}")
    outside = data.get("outside_edits", [])
    if not isinstance(outside, list):
        errors.append('"outside_edits" must be a list')
        outside = []
    for n, o in enumerate(outside):
        if not isinstance(o, dict) or not is_text(o.get("path"), 1) or not is_text(o.get("why"), 5):
            errors.append(f'outside_edits[{n}]: needs a "path" and a "why"')
    return errors, seen, outside


def cmd_fixes(d, args):
    plan = load_plan(d)
    ex = d / "exhaustive"
    findings, err = read_json(ex / "findings.json")
    if err:
        die(f"{ex}/findings.json: {err}; run `exhaustive.py review` until it passes first")
    by_batch = {}
    for f in findings:
        by_batch.setdefault(f["batch"], []).append(f)
    if args.batch and args.batch not in by_batch:
        die(f"batch {args.batch} has no findings")
    batches = [args.batch] if args.batch else list(by_batch)

    bad, results, outside = {}, {}, []
    for bid in batches:
        errors, res, out = check_fix(d, bid, by_batch[bid])
        if errors:
            bad[bid] = errors
        results.update(res)
        outside += [dict(o, batch=bid) for o in out if isinstance(o, dict)]
    if bad:
        print_failures(bad, len(batches), "batches", "start a new fixer for")
        return 1

    def count(status, fs):
        return sum(1 for f in fs if results[f["id"]]["status"] == status)

    if args.batch:
        fs = by_batch[args.batch]
        print(f"{args.batch}: ok, {count('fixed', fs)} fixed, {count('report-only', fs)} report-only, "
              f"{count('rejected', fs)} rejected")
        return 0

    rows = []
    by_section = {}
    for f in findings:
        by_section.setdefault(f["section"], []).append(f)
    for s in plan["sections"]:
        fs = by_section.get(s["id"], [])
        fixed, reported, rejected = count("fixed", fs), count("report-only", fs), count("rejected", fs)
        result = "changes" if fixed or reported else "clean"
        if rejected:
            result += f" ({rejected} rejected)"
        rows.append(f"| {s['guide']} § {s['heading']} | {result} | {fixed} | {reported} |")
    for g in plan["main_agent_guides"]:
        rows.append(f"| {g} | <fill in: checked by the main agent> | | |")
    (ex / "report-rows.md").write_text("\n".join(rows) + "\n")

    by_id = {s["id"]: s for s in plan["sections"]}
    lines = []
    for f in findings:
        s, r = by_id[f["section"]], results[f["id"]]
        outcome = {"fixed": "fixed in patch", "report-only": f"report only: {r.get('note')}",
                   "rejected": f"rejected: {r.get('note')}"}[r["status"]]
        lines.append(f"{s['guide']} § {s['heading']} — `{f['file']}:{f['line']}` — {f['problem']} — {outcome}")
        if r["status"] == "fixed" and r.get("note"):
            lines.append(f"  Note: {r['note']}")
        if r.get("choice"):
            lines.append(f"  Choice: {r['choice']}")
    (ex / "findings.md").write_text("".join(f"{l}\n" for l in lines))
    (ex / "outside-edits.md").write_text("".join(f"{o['path']} — {o['why']} (batch {o['batch']})\n" for o in outside))

    total = len(findings)
    print(f"all {len(batches)} batches pass: {count('fixed', findings)} of {total} findings fixed, "
          f"{count('report-only', findings)} report-only, {count('rejected', findings)} rejected; "
          f"{len(outside)} edits left to the main agent")
    print(f"report rows:   {ex}/report-rows.md")
    print(f"findings:      {ex}/findings.md")
    print(f"outside edits: {ex}/outside-edits.md")
    return 0


# ---------------------------------------------------------------------------------------------------------------------
# Recheck of the fixes
# ---------------------------------------------------------------------------------------------------------------------


def cmd_recheck(d, _args):
    review = load_review(d)
    sub = d / "recheck"
    if sub.exists():
        die(f"{sub} already exists; remove it to recheck again")
    repo, base = review["REPO"], review["BASELINE"]

    def git(*a, env=None):
        return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True,
                              env=env).stdout.splitlines()

    cpp = re.compile(r"\.(h|hh|hpp|c|cc|cpp|cxx|ipp|tpp)$")
    # The whole working tree (tracked changes and untracked files) in a throwaway index, so the user's index, which
    # may or may not hold staged changes, does not matter.
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        real = Path(repo, git("rev-parse", "--git-path", "index")[0])
        if real.is_file():
            shutil.copy(real, index)
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        git("add", "-A", env=env)
        changed = git("diff", "--cached", "--name-only", "--diff-filter=d", "--no-renames", base, env=env)
    in_base = set(git("ls-tree", "-r", "--name-only", base))
    files = sorted(f for f in changed if cpp.search(f) and (Path(repo) / f).is_file())
    new = [f for f in files if f not in in_base]
    if not files:
        print("nothing changed since the baseline: no recheck needed")
        return 0
    sub.mkdir()
    values = dict(review)
    values.update({"MODE": "diff", "DIFF_FROM": base, "NEW_FILES": ",".join(f for f in files if f in set(new)),
                   "EXHAUSTIVE": "1"})
    (sub / "review.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    (sub / "scope.txt").write_text("".join(f"{f}\n" for f in files))
    print(f"recheck of {len(files)} file(s) changed since the baseline, in {sub}")
    return cmd_plan(sub, None)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "review", "fixes", "recheck"):
        sp = sub.add_parser(name)
        sp.add_argument("results_dir", type=Path, help="the review's results directory, as setup.sh printed it")
        if name == "review":
            sp.add_argument("--item", help="check only this work item's ledger")
        if name == "fixes":
            sp.add_argument("--batch", help="check only this batch's fix result")
    a = p.parse_args()
    d = a.results_dir.resolve()
    if not d.is_dir():
        die(f"no results directory {d}")
    sys.exit({"plan": cmd_plan, "review": cmd_review, "fixes": cmd_fixes,
          "recheck": cmd_recheck}[a.cmd](d, a))


if __name__ == "__main__":
    main()
