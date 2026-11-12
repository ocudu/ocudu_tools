# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Tests for resolve_diff.sh.

Run from the repo root:
    python3 -m unittest discover -s tests -p "test_*.py"
    python3 tests/code_review/test_resolve_diff.py
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
SCRIPT = TEST_DIR.parents[1] / "skills" / "ocudu-code-review" / "scripts" / "resolve_diff.sh"
NUMBERED_DIFF = SCRIPT.parent / "numbered_diff.sh"

# Git hooks export GIT_INDEX_FILE and the like, which would point the test repos at the caller's.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, env=GIT_ENV, check=True,
        capture_output=True, text=True,
    ).stdout


def commit(root, name, text):
    (Path(root) / name).write_text(text)
    git(root, "add", name)
    git(root, "commit", "-m", f"add {name}")


class ResolveDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        git(self.root, "init", "-q", "-b", "dev", ".")
        commit(self.root, "base.txt", "base\n")

    def run_script(self, *args, cwd=None):
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=cwd or self.root, env=GIT_ENV,
            capture_output=True, text=True,
        )

    def resolve(self, *args, cwd=None):
        result = self.run_script(*args, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = {"PLAN": [], "FILE": [], "GROUP": []}
        in_plan = False
        for line in result.stdout.splitlines():
            if line == "PLAN:":
                in_plan = True
            elif in_plan:
                if line.startswith("- "):
                    out["PLAN"].append(line[2:])
            elif line:
                key, _, value = line.partition("=")
                if key in ("FILE", "GROUP"):
                    out[key].append(value)
                else:
                    out[key] = value
        return out

    # A local base has no remote to fetch from, so it is used as-is.
    def make_local_base(self):
        git(self.root, "branch", "local-base")
        return "--base=local-base"

    def test_no_target_diffs_base_against_head(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "f.txt", "f\n")
        out = self.resolve(base)
        self.assertEqual(out["BASE"], "local-base")
        self.assertEqual(out["TIP"], "HEAD")
        self.assertEqual(out["CMD"], "git diff local-base...HEAD")
        self.assertEqual(out["EMPTY"], "0")

    def test_arguments_may_arrive_as_one_string(self):
        git(self.root, "branch", "local-base")
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "f.txt", "f\n")
        out = self.resolve("feature --base=local-base")
        self.assertEqual(out["CMD"], "git diff local-base...feature")
        self.assertEqual(out["TIP"], "feature")

    def test_branch_target_is_the_tip(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "f.txt", "f\n")
        git(self.root, "checkout", "-q", "dev")
        out = self.resolve("feature", base)
        self.assertEqual(out["CMD"], "git diff local-base...feature")

    def test_working_and_staged_need_no_base(self):
        (Path(self.root) / "dirty.txt").write_text("x\n")
        git(self.root, "add", "dirty.txt")
        working = self.resolve("working")
        self.assertEqual(working["CMD"], "git diff HEAD")
        self.assertEqual(working["BASE"], "")
        self.assertEqual(working["TIP"], "")
        self.assertEqual(working["EMPTY"], "0")
        self.assertEqual(self.resolve("staged")["CMD"], "git diff --staged")

    def test_explicit_range_passes_through_unchanged(self):
        commit(self.root, "second.txt", "s\n")
        out = self.resolve("HEAD~1..HEAD")
        self.assertEqual(out["CMD"], "git diff HEAD~1..HEAD")
        self.assertEqual(out["BASE"], "")

    def test_base_is_ignored_and_flagged_for_local_only_targets(self):
        out = self.resolve("working", "--base=local-base")
        self.assertEqual(out["BASE"], "")
        self.assertTrue(any("ignored" in step for step in out["PLAN"]), out["PLAN"])

    def test_empty_diff_is_reported(self):
        base = self.make_local_base()
        self.assertEqual(self.resolve(base)["EMPTY"], "1")

    def test_dirty_tree_is_reported(self):
        base = self.make_local_base()
        self.assertEqual(self.resolve(base)["DIRTY"], "0")
        (Path(self.root) / "base.txt").write_text("changed\n")
        self.assertEqual(self.resolve(base)["DIRTY"], "1")

    def test_stale_remote_base_is_fetched(self):
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        git(remote.name, "init", "-q", "--bare", "-b", "dev", ".")
        git(self.root, "remote", "add", "origin", remote.name)
        git(self.root, "push", "-q", "origin", "dev")
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "f.txt", "f\n")

        # A second clone advances origin/dev behind this repo's back.
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        git(other.name, "clone", "-q", remote.name, ".")
        commit(other.name, "upstream.txt", "u\n")
        git(other.name, "push", "-q", "origin", "dev")

        out = self.resolve()
        self.assertEqual(out["CMD"], "git diff origin/dev...HEAD")
        head = git(self.root, "rev-parse", "origin/dev").strip()
        self.assertEqual(head, git(other.name, "rev-parse", "HEAD").strip())

    def make_glab(self, body, status=0, discussions=None):
        """Put a `glab` stub first on PATH; returns the env to run with.

        `discussions` answers the review-thread endpoint; without it that
        endpoint reports no threads.
        """
        bindir = tempfile.TemporaryDirectory()
        self.addCleanup(bindir.cleanup)
        threads = "[]" if discussions is None else discussions
        stub = Path(bindir.name) / "glab"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            f"  *discussions*page=1*) printf '%s' '{threads}'; exit 0 ;;\n"
            "  *discussions*) printf '[]'; exit 0 ;;\n"
            f"esac\nprintf '%s' '{body}'\nexit {status}\n"
        )
        stub.chmod(0o755)
        return {**GIT_ENV, "PATH": f"{bindir.name}:{GIT_ENV['PATH']}"}

    def make_mr(self, iid=7, target="dev"):
        """A remote holding the MR head under refs/merge-requests/<iid>/head."""
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        git(remote.name, "init", "-q", "--bare", "-b", "dev", ".")
        git(self.root, "remote", "add", "origin", remote.name)
        git(self.root, "push", "-q", "origin", "dev")
        if target != "dev":
            git(self.root, "push", "-q", "origin", f"HEAD:refs/heads/{target}")
        git(self.root, "checkout", "-q", "-b", "mr-work")
        commit(self.root, "mr.txt", "mr\n")
        head = git(self.root, "rev-parse", "HEAD").strip()
        git(self.root, "push", "-q", "origin", f"HEAD:refs/merge-requests/{iid}/head")
        git(self.root, "checkout", "-q", "dev")
        git(self.root, "branch", "-q", "-D", "mr-work")
        return f"https://gitlab.com/ocudu/ocudu/-/merge_requests/{iid}", head

    def test_merge_request_base_comes_from_glab(self):
        url, _ = self.make_mr(iid=9, target="release")
        env = self.make_glab('{"target_branch":"release","iid":9}')
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASE=origin/release", result.stdout)
        self.assertIn("CMD=git diff origin/release...refs/ocudu-code-review/mr-9", result.stdout)
        self.assertNotIn("glab could not read", result.stdout)

    def test_merge_request_falls_back_when_glab_fails(self):
        url, _ = self.make_mr(iid=9)
        env = self.make_glab("", status=1)
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASE=origin/dev", result.stdout)
        self.assertIn("- MR !9: glab could not read its target branch", result.stdout)

    def test_explicit_base_wins_over_glab(self):
        url, _ = self.make_mr(iid=9)
        env = self.make_glab('{"target_branch":"release"}')
        result = subprocess.run(
            ["bash", str(SCRIPT), url, "--base=origin/dev"], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BASE=origin/dev", result.stdout)

    def test_merge_request_review_threads_reach_the_plan(self):
        url, _ = self.make_mr(iid=9)
        threads = json.dumps([{"notes": [{
            "body": "Bounds check missing",
            "author": {"username": "ana"},
            "system": False,
            "resolved": False,
            "position": {"new_path": "mr.txt", "new_line": 1},
        }]}])
        env = self.make_glab('{"target_branch":"dev"}', discussions=threads)
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COMMENT=mr.txt:1 @ana: Bounds check missing", result.stdout)
        self.assertIn("- 1 reviewer thread is already on this MR", result.stdout)
        digest = Path(self.root) / ".git" / "ocudu-code-review" / "mr-9-comments.md"
        self.assertIn("Bounds check missing", digest.read_text())

    def test_merge_request_without_review_threads_says_nothing(self):
        url, _ = self.make_mr(iid=9)
        env = self.make_glab('{"target_branch":"dev"}')
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("COMMENT=", result.stdout)
        self.assertNotIn("reviewer thread", result.stdout)

    def test_unreadable_review_threads_are_flagged(self):
        url, _ = self.make_mr(iid=9)
        # `glab` answers the MR itself but not its discussions.
        bindir = tempfile.TemporaryDirectory()
        self.addCleanup(bindir.cleanup)
        stub = Path(bindir.name) / "glab"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *discussions*) echo 'no token' >&2; exit 1 ;;\n"
            "esac\n"
            "printf '%s' '{\"target_branch\":\"dev\"}'\n"
        )
        stub.chmod(0o755)
        env = {**GIT_ENV, "PATH": f"{bindir.name}:{GIT_ENV['PATH']}"}
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not read its review threads (glab api failed: no token)", result.stdout)

    def test_merge_request_from_another_project_names_both(self):
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        git(remote.name, "init", "-q", "--bare", "-b", "dev", ".")
        git(self.root, "remote", "add", "origin", remote.name)
        git(self.root, "push", "-q", "origin", "dev")
        result = subprocess.run(
            ["bash", str(SCRIPT), "https://gitlab.com/ocudu/ocudu/-/merge_requests/7",
             "--base=origin/dev"],
            cwd=self.root, env=GIT_ENV, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"origin is {remote.name} ", result.stderr)
        self.assertIn("but the MR is in ocudu/ocudu", result.stderr)

    def test_merge_request_review_prunes_old_reviews(self):
        url, _ = self.make_mr(iid=7)
        state = Path(self.root) / ".git" / "ocudu-code-review"
        state.mkdir(parents=True)
        head = git(self.root, "rev-parse", "HEAD").strip()
        # A ref from before stamps, one of a recent review, and one whose stamp is two days old.
        for iid in (1, 2, 3):
            git(self.root, "update-ref", f"refs/ocudu-code-review/mr-{iid}", head)
        (state / "mr-2.stamp").touch()
        old = state / "mr-3.stamp"
        old.touch()
        two_days_ago = old.stat().st_mtime - 2 * 86400
        os.utime(old, (two_days_ago, two_days_ago))
        (state / "mr-3-comments.md").write_text("old digest\n")
        os.utime(state / "mr-3-comments.md", (two_days_ago, two_days_ago))

        env = self.make_glab('{"target_branch":"dev"}')
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=self.root, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        refs = git(self.root, "for-each-ref", "--format=%(refname)", "refs/ocudu-code-review/").split()
        self.assertEqual(sorted(refs), ["refs/ocudu-code-review/mr-2", "refs/ocudu-code-review/mr-7"])
        self.assertFalse((state / "mr-3-comments.md").exists())
        self.assertTrue((state / "mr-7.stamp").exists())
        # glab answered, so its empty error output is not kept.
        self.assertFalse((state / "mr-7-comments.err").exists())

    # Worktrees share the MR refs, so a review in one must see the stamps of reviews in another.
    def test_merge_request_review_keeps_reviews_of_other_worktrees(self):
        url, _ = self.make_mr(iid=7)
        head = git(self.root, "rev-parse", "HEAD").strip()
        git(self.root, "update-ref", "refs/ocudu-code-review/mr-2", head)
        state = Path(self.root) / ".git" / "ocudu-code-review"
        state.mkdir(parents=True)
        (state / "mr-2.stamp").touch()
        worktree = Path(self.root) / "wt"
        git(self.root, "worktree", "add", "-q", "--detach", str(worktree))

        env = self.make_glab('{"target_branch":"dev"}')
        result = subprocess.run(
            ["bash", str(SCRIPT), url], cwd=worktree, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        refs = git(self.root, "for-each-ref", "--format=%(refname)", "refs/ocudu-code-review/").split()
        self.assertEqual(sorted(refs), ["refs/ocudu-code-review/mr-2", "refs/ocudu-code-review/mr-7"])
        self.assertTrue((state / "mr-7.stamp").exists())

    def test_merge_request_url_resolves_to_its_head(self):
        url, mr_head = self.make_mr(iid=7)
        out = self.resolve(url, "--base=origin/dev")
        self.assertEqual(out["TIP"], "refs/ocudu-code-review/mr-7")
        self.assertEqual(out["CMD"], "git diff origin/dev...refs/ocudu-code-review/mr-7")
        self.assertEqual(git(self.root, "rev-parse", out["TIP"]).strip(), mr_head)

    def test_size_accounting(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "a.txt", "".join(f"line {i}\n" for i in range(10)))
        commit(self.root, "b.txt", "one\n")
        out = self.resolve(base)
        self.assertEqual(out["FILES"], "2")
        self.assertEqual(out["LINES"], "11")
        self.assertEqual(out["BIG"], "0")
        self.assertGreater(int(out["BYTES"]), 0)

    def test_empty_diff_reports_zero_sizes(self):
        out = self.resolve(self.make_local_base())
        self.assertEqual(out["EMPTY"], "1")
        self.assertEqual(out["FILES"], "0")
        self.assertEqual(out["LINES"], "0")
        self.assertEqual(out["BYTES"], "0")
        self.assertEqual(out["BIG"], "0")

    def test_whitespace_only_churn_shows_in_lines_nows(self):
        body = "".join(f"line {i}\n" for i in range(20))
        commit(self.root, "ws.txt", body)
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "ws.txt", body.replace("line", "    line"))
        out = self.resolve(base)
        self.assertEqual(out["LINES"], "40")
        self.assertEqual(out["LINES_NOWS"], "0")

    def test_big_diff_is_flagged(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "big.txt", "".join(f"a fairly long line of text {i}\n" for i in range(4000)))
        out = self.resolve(base)
        self.assertEqual(out["BIG"], "1")
        self.assertGreater(int(out["BYTES"]), 61440)

    def test_fanout_flag_is_reported(self):
        base = self.make_local_base()
        self.assertEqual(self.resolve(base)["FANOUT"], "0")
        self.assertEqual(self.resolve(base, "--fanout")["FANOUT"], "1")

    def test_plan_reads_a_small_diff_whole(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "a.txt", "one\n")
        plan = self.resolve(base)["PLAN"]
        self.assertEqual(len(plan), 1)
        self.assertIn(f"Read the whole diff: `{NUMBERED_DIFF} local-base...HEAD`", plan[0])

    def test_numbered_diff_gives_new_file_lines(self):
        commit(self.root, "n.txt", "".join(f"line {i}\n" for i in range(1, 11)))
        git(self.root, "branch", "local-base")
        commit(self.root, "n.txt", "".join(f"line {i}\n" for i in range(1, 11) if i != 5).replace("line 8", "line 8b"))
        out = subprocess.run(["bash", str(NUMBERED_DIFF), "local-base...HEAD"], cwd=self.root, env=GIT_ENV,
                             check=True, capture_output=True, text=True).stdout
        body = out.splitlines()
        # Context and added lines carry their new-file line, removed ones none.
        self.assertIn("    4  line 4", body)
        self.assertIn("    - -line 5", body)
        self.assertIn("    7 +line 8b", body)
        self.assertIn("    - -line 8", body)

    def test_plan_for_a_big_diff_ranks_files_and_withholds_the_whole_read(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "small.txt", "one\n")
        commit(self.root, "big.txt", "".join(f"a fairly long line of text {i}\n" for i in range(4000)))
        out = self.resolve(base)
        self.assertEqual(out["BIG"], "1")
        self.assertEqual([f.split(" ", 1)[1] for f in out["FILE"]], ["big.txt", "small.txt"])
        self.assertTrue(any("do not read it whole" in step for step in out["PLAN"]), out["PLAN"])
        # A merely large diff is reviewed inline without mentioning --fanout.
        self.assertFalse(any("--fanout" in step for step in out["PLAN"]), out["PLAN"])

    def test_plan_asks_about_fanout_only_for_a_huge_diff(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "huge.txt", "".join(f"a fairly long line of text {i}\n" for i in range(10000)))
        out = self.resolve(base)
        self.assertGreater(int(out["BYTES"]), 204800)
        self.assertTrue(any("Ask the user whether to re-run with `--fanout`" in step
                            for step in out["PLAN"]), out["PLAN"])

    def test_plan_groups_files_when_fanout_is_on(self):
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        for i in range(8):
            commit(self.root, f"f{i}.txt", "".join(f"a fairly long line of text {j}\n" for j in range(400)))
        out = self.resolve(base, "--fanout")
        self.assertEqual(out["BIG"], "1")
        self.assertEqual(out["FILE"], [])
        grouped = [path for g in out["GROUP"] for path in g.split(" ")[1:]]
        self.assertCountEqual(grouped, [f"f{i}.txt" for i in range(8)])
        self.assertTrue(any("subagent" in step for step in out["PLAN"]), out["PLAN"])

    def test_plan_stops_on_an_empty_diff(self):
        plan = self.resolve(self.make_local_base())["PLAN"]
        self.assertEqual(len(plan), 1)
        self.assertIn("No changes to review", plan[0])

    def test_plan_flags_a_dirty_tree_and_reformatting(self):
        body = "".join(f"line {i}\n" for i in range(20))
        commit(self.root, "ws.txt", body)
        base = self.make_local_base()
        git(self.root, "checkout", "-q", "-b", "feature")
        commit(self.root, "ws.txt", body.replace("line", "    line"))
        (Path(self.root) / "ws.txt").write_text(body.replace("line", "\tline"))
        plan = self.resolve(base)["PLAN"]
        self.assertTrue(any("working tree is dirty" in step for step in plan), plan)
        self.assertTrue(any("--ignore-all-space" in step for step in plan), plan)

    def test_help(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: resolve_diff.sh", result.stdout)
        self.assertIn("GitLab MR URL", result.stdout)

    def test_errors(self):
        cases = {
            "unknown flag": ["--deps"],
            "more than one target": ["a", "b"],
            "target ref not found": ["no-such-branch", "--base=local-base"],
            "base ref not found": ["--base=no-such-base"],
            "no merge_requests": ["https://gitlab.com/ocudu/ocudu/-/issues/3"],
        }
        self.make_local_base()
        for expected, args in cases.items():
            with self.subTest(expected):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(expected, result.stderr)

    def test_outside_a_work_tree(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        env = {**GIT_ENV, "GIT_CEILING_DIRECTORIES": outside.name}
        result = subprocess.run(
            ["bash", str(SCRIPT)], cwd=outside.name, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not inside a git work tree", result.stderr)


if __name__ == "__main__":
    unittest.main()
