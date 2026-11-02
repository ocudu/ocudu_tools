#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tool checks of the guidelines review: hex literals and include-what-you-use, over a review's scope.

    tool_checks.py <results-dir> [--jobs <n>]

Reads <results-dir>/review.env and scope.txt, both written by setup.sh, and writes:

    <results-dir>/tool-checks/summary.md      one table row per check plus per-file details, for report.md
    <results-dir>/tool-checks/hex.txt         every line whose hex literals were lowercased, before the change
    <results-dir>/tool-checks/iwyu/<tu>.log   the raw iwyu output (and command) of each translation unit

Hex literals are fixed in place, in the checkout, with the expression of OCUDU's hex2lowercase.sh restricted to the
scoped files. iwyu only reports: its output is input to the code guide's include rules, not a verdict.

iwyu runs on each scoped source file, and on the source file that implements each scoped header with
--check_also=<header>. The compile flags come from the build tree's compile_commands.json, minus -c, -o <file>,
dependency-file flags and the GCC-only flags clang rejects. A build tree without one is configured: an existing tree
keeps its settings and only gains CMAKE_EXPORT_COMPILE_COMMANDS; a missing one is configured as a Debug build.

A tool that is missing or fails is recorded as that check's result; the exit status is 0 unless the arguments are wrong.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HEADER_EXTS = {".h", ".hh", ".hpp", ".ipp", ".tpp"}
SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx"}

# The expression of .gitlab/ci/static/hex2lowercase.sh: sed 's/0x[0-9a-fA-F]*/\L&/g'.
HEX_RE = re.compile(r"0x[0-9a-fA-F]*")
HEX_SKIP_DIRS = {"bundled", "external"}

# GCC-only flags clang rejects. Anything else clang rejects is found from its "unknown argument" errors and dropped on
# a retry, so this list only saves the retries.
GCC_ONLY_PREFIXES = (
    "-fconcepts-diagnostics-depth",
    "-fmax-errors",
    "-fno-tree-",
    "-ftree-",
    "-fno-var-tracking-assignments",
    "-fipa-",
    "-fno-ipa-",
)
# Flags that take the next argument as their value and only matter for a real compile.
DROP_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ"}
DROP_ALONE = {"-c", "-MD", "-MMD", "-MP"}
UNKNOWN_ARG_RE = re.compile(r"error: (?:unknown argument|unsupported option|unknown argument ignored)[^']*'([^']+)'")
MAX_RETRIES = 5
IWYU_TIMEOUT_S = 900

INCLUDE_DIR_FLAGS = ("-isystem", "-iquote", "-I")
INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


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


# ---------------------------------------------------------------------------------------------------------------------
# Hex literals
# ---------------------------------------------------------------------------------------------------------------------


def check_hex(repo, files, out_dir):
    """Lowercases hex literals in the scoped files. Returns (changed literals, changed files, result text)."""
    changed_literals = 0
    changed_files = 0
    log = []
    for rel in files:
        path = repo / rel
        # Third-party code, which CI's hex2lowercase.sh leaves alone too.
        if not path.is_file() or HEX_SKIP_DIRS & set(Path(rel).parts[:-1]):
            continue
        # newline="" keeps CRLF files byte-identical; surrogateescape keeps non-UTF-8 bytes.
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
            lines = f.readlines()
        new_lines = []
        file_changed = False
        for lineno, line in enumerate(lines, 1):
            new = HEX_RE.sub(lambda m: m.group(0).lower(), line)
            if new != line:
                changed_literals += sum(1 for m in HEX_RE.finditer(line) if m.group(0) != m.group(0).lower())
                log.append(f"{rel}:{lineno}: {line.rstrip()}")
                file_changed = True
            new_lines.append(new)
        if file_changed:
            # Only changed files are rewritten, so the build does not redo what the check left alone.
            with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                f.writelines(new_lines)
            changed_files += 1

    (out_dir / "hex.txt").write_text("".join(f"{entry}\n" for entry in log))
    if changed_literals == 0:
        return 0, 0, "ok (no uppercase hex literals)"
    return changed_literals, changed_files, f"changes: {changed_literals} literals in {changed_files} files"


# ---------------------------------------------------------------------------------------------------------------------
# include-what-you-use
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Unit:
    """One iwyu run: a translation unit and the scoped files it reports on."""

    source: Path  # absolute
    entry: dict
    report_on: list = field(default_factory=list)  # scoped files (relative) this run covers
    check_also: list = field(default_factory=list)  # scoped headers (relative)


@dataclass
class FileResult:
    status: str = "not checked"
    add: int = 0
    remove: int = 0
    via: str = ""
    log: str = ""


def ensure_compile_commands(repo, build, out_dir):
    db = build / "compile_commands.json"
    if db.is_file():
        return db, None
    if not shutil.which("cmake"):
        return None, "cmake not found"
    if (build / "CMakeCache.txt").is_file():
        # The developer's own tree: keep its generator, build type and options.
        cmd = ["cmake", "-B", str(build), "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]
    else:
        generator = ["-G", "Ninja"] if shutil.which("ninja") else []
        cmd = ["cmake", "-S", str(repo), "-B", str(build), *generator, "-DCMAKE_BUILD_TYPE=Debug",
               "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]
    log = out_dir / "cmake.log"
    with open(log, "w") as f:
        f.write(f"$ {shlex.join(cmd)}\n")
        f.flush()
        rc = subprocess.run(cmd, cwd=repo, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0 or not db.is_file():
        return None, f"configure failed, see {log}"
    return db, None


def load_compile_commands(db):
    entries = {}
    for entry in json.loads(db.read_text()):
        src = Path(entry["directory"], entry["file"]).resolve()
        # A file built twice (e.g. in two targets) keeps its first entry; the flags differ only in defines.
        entries.setdefault(src, entry)
    return entries


def include_dirs(entry):
    """The -I, -iquote and -isystem directories of a compile_commands entry, absolute."""
    args = entry.get("arguments") or shlex.split(entry["command"])
    dirs = []
    for i, a in enumerate(args):
        for flag in INCLUDE_DIR_FLAGS:
            if a == flag and i + 1 < len(args):
                dirs.append(args[i + 1])
            elif a.startswith(flag) and len(a) > len(flag):
                dirs.append(a[len(flag):])
            else:
                continue
            break
    return [Path(entry["directory"], d) for d in dirs]


def includes_header(source, header, entry):
    """True if `source` has an #include that resolves to `header`, searched as the compiler does: the source's directory,
    then the entry's include directories."""
    try:
        text = source.read_text(errors="replace")
    except OSError:
        return False
    search = [source.parent, *include_dirs(entry)]
    for inc in INCLUDE_RE.findall(text):
        found = next((p for p in (d / inc for d in search) if p.is_file()), None)
        if found is not None and found.resolve() == header:
            return True
    return False


def trailing_dir_match(a, b):
    """Number of trailing directory names that a and b share (include/ocudu/x/y vs lib/x/y -> 2)."""
    n = 0
    for x, y in zip(reversed(a.parent.parts), reversed(b.parent.parts)):
        if x != y:
            break
        n += 1
    return n


def implementing_source(header, entries):
    """The source file that implements `header`: same stem, closest directory, and it includes the header. Falls back
    to any source file that includes it."""
    same_stem = [s for s in entries if s.stem == header.stem]
    same_stem.sort(key=lambda s: (s.parent != header.parent, -trailing_dir_match(s, header), str(s)))
    for s in same_stem:
        if includes_header(s, header, entries[s]):
            return s
    for s in sorted(entries):
        if s.suffix in SOURCE_EXTS and includes_header(s, header, entries[s]):
            return s
    return None


def iwyu_args(entry, source):
    """The clang flags of a compile_commands entry, ready for iwyu."""
    args = entry.get("arguments") or shlex.split(entry["command"])
    # Drop the compiler; a launcher (ccache) puts the real compiler in second place.
    drop = 2 if args and os.path.basename(args[0]).startswith("ccache") else 1
    args = args[drop:]
    out = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in DROP_WITH_VALUE:
            skip = True
            continue
        if a in DROP_ALONE or (a.startswith("-o") and len(a) > 2) or a.startswith(GCC_ONLY_PREFIXES):
            continue
        if not a.startswith("-") and Path(entry["directory"], a).resolve() == source:
            continue
        out.append(a)
    return out


def run_iwyu(unit, log_path):
    args = iwyu_args(unit.entry, unit.source)
    extra = ["-Wno-unknown-warning-option"]
    for hdr in unit.check_also:
        extra += ["-Xiwyu", f"--check_also=*/{hdr}"]
    dropped = []
    for _ in range(MAX_RETRIES):
        cmd = ["include-what-you-use", *extra, *args, str(unit.source)]
        try:
            proc = subprocess.run(cmd, cwd=unit.entry["directory"], capture_output=True, text=True,
                                  timeout=IWYU_TIMEOUT_S)
            output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            output = f"error: timed out after {IWYU_TIMEOUT_S} s"
            break
        unknown = set(UNKNOWN_ARG_RE.findall(output))
        if not unknown:
            break
        dropped += sorted(unknown)
        args = [a for a in args if a not in unknown]
    with open(log_path, "w") as f:
        f.write(f"$ cd {unit.entry['directory']} && {shlex.join(cmd)}\n")
        if dropped:
            f.write(f"# dropped flags clang rejected: {' '.join(dropped)}\n")
        f.write(output)
    return output


def parse_iwyu(output, cwd, repo):
    """Per reported file (relative to repo): [status, lines to add, lines to remove], status 'clean' or 'changes'.
    Returns (results, error lines)."""

    def rel(p):
        path = Path(cwd, p).resolve()
        try:
            return path.relative_to(repo).as_posix()
        except ValueError:
            return path.as_posix()

    results = {}
    errors = [l for l in output.splitlines() if re.search(r"(^|: )(fatal )?error: ", l)]
    current = None
    for line in output.splitlines():
        m = re.match(r"^(.*) should (add|remove) these lines:$", line)
        if m:
            current = (rel(m.group(1)), m.group(2))
            results.setdefault(current[0], ["changes", 0, 0])
            continue
        m = re.match(r"^\((.*) has correct #includes/fwd-decls\)$", line)
        if m:
            results.setdefault(rel(m.group(1)), ["clean", 0, 0])
            current = None
            continue
        if not line.strip() or line.startswith("The full include-list for ") or line.startswith("---"):
            current = None
            continue
        if current:
            name, kind = current
            if kind == "add":
                results[name][1] += 1
            elif line.startswith("- "):
                results[name][2] += 1
    return results, errors


def display(path, repo):
    return (path.relative_to(repo) if path.is_relative_to(repo) else path).as_posix()


def check_iwyu(repo, build, files, out_dir, jobs):
    """Returns (result text, {scoped file: FileResult})."""
    scoped = [f for f in files if Path(f).suffix in HEADER_EXTS | SOURCE_EXTS and (repo / f).is_file()]
    per_file = {f: FileResult() for f in scoped}
    if not scoped:
        return "n/a: no C/C++ files in scope", per_file
    if not shutil.which("include-what-you-use"):
        return "failed: include-what-you-use not found", per_file

    db, err = ensure_compile_commands(repo, build, out_dir)
    if err:
        return f"failed: {err}", per_file
    entries = load_compile_commands(db)

    units = {}
    for f in scoped:
        path = (repo / f).resolve()
        if path.suffix in SOURCE_EXTS:
            if path not in entries:
                per_file[f].status = "not checked: not in compile_commands.json (not built in this configuration)"
                continue
            units.setdefault(path, Unit(path, entries[path])).report_on.append(f)
        else:
            src = implementing_source(path, entries)
            if src is None:
                per_file[f].status = "not checked: no source file in compile_commands.json includes it"
                continue
            unit = units.setdefault(src, Unit(src, entries[src]))
            unit.report_on.append(f)
            unit.check_also.append(f)
            per_file[f].via = display(src, repo)

    log_dir = out_dir / "iwyu"
    log_dir.mkdir(exist_ok=True)

    def work(unit):
        log = log_dir / (display(unit.source, repo).replace("/", "__") + ".log")
        return unit, log, run_iwyu(unit, log)

    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for unit, log, output in pool.map(work, units.values()):
            results, errors = parse_iwyu(output, unit.entry["directory"], repo)
            log_rel = os.path.relpath(log, out_dir.parent)
            for f in unit.report_on:
                fr = per_file[f]
                fr.log = log_rel
                if f in results:
                    fr.status, fr.add, fr.remove = results[f]
                elif errors:
                    fr.status = f"failed: {errors[0].strip()}"
                else:
                    fr.status = "failed: iwyu reported nothing for this file"
            if errors:
                failed += 1

    checked = [r for r in per_file.values() if r.status in ("clean", "changes")]
    changes = sum(1 for r in checked if r.status == "changes")
    not_ok = len(per_file) - len(checked)
    parts = []
    if changes:
        parts.append(f"changes suggested for {changes} files")
    if not_ok:
        parts.append(f"{not_ok} files not checked or failed")
    if failed:
        parts.append(f"{failed} of {len(units)} iwyu runs had compile errors")
    if not parts:
        return f"ok ({len(checked)} files clean)", per_file
    head = "failed" if not checked else "changes" if changes else "partial"
    return f"{head}: {'; '.join(parts)}", per_file


# ---------------------------------------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------------------------------------


def write_summary(out_dir, hex_result, iwyu_result):
    n_lit, n_files, hex_text = hex_result
    iwyu_text, per_file = iwyu_result
    lines = [
        "# Tool checks",
        "",
        "| Check | Result | Fixed | Reported |",
        "| ----- | ------ | ----- | -------- |",
        f"| hex literals | {hex_text} | {n_lit} | 0 |",
        f"| iwyu | {iwyu_text} | <fill in> | <fill in> |",
        "",
        "## hex literals",
        "",
        "Fixed in the checkout. Lines before the change: `tool-checks/hex.txt`." if n_lit else "Nothing to fix.",
        "",
        "## iwyu",
        "",
        "Suggestions only; apply them through the code guide's include rules (transitive includes are fine, remove",
        "unused includes, prefer forward declarations). Fixed / Reported in the table above are filled in after that.",
        "",
        "| File | Result | Add | Remove | Checked through | Log |",
        "| ---- | ------ | --- | ------ | --------------- | --- |",
    ]
    for f, r in per_file.items():
        lines.append(f"| `{f}` | {r.status} | {r.add} | {r.remove} | {r.via and f'`{r.via}`'} | {r.log} |")
    text = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(text)
    return text


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("results_dir", type=Path, help="the review's results directory, as setup.sh printed it")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    a = p.parse_args()

    results_dir = a.results_dir.resolve()
    review = load_review(results_dir)
    repo = Path(review["REPO"]).resolve()
    build = Path(review["BUILD"]).resolve()
    scope = results_dir / "scope.txt"
    if not scope.is_file():
        die(f"{scope} not found; run setup.sh first")
    files = [l.strip() for l in scope.read_text().splitlines() if l.strip()]
    if not files:
        die(f"{scope} is empty")

    out_dir = results_dir / "tool-checks"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Hex first: iwyu then sees the files as they will be reviewed.
    hex_result = check_hex(repo, files, out_dir)
    iwyu_result = check_iwyu(repo, build, files, out_dir, max(1, a.jobs))
    sys.stdout.write(write_summary(out_dir, hex_result, iwyu_result))
    print(f"\nwritten: {out_dir}/summary.md")


if __name__ == "__main__":
    main()
