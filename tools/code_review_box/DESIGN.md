# Review box design

Container for reviewing OCUDU code with Claude Code. Living document: decisions are updated as they are agreed. Usage is
in the README.

## Scope

- Local reviews of OCUDU code: code guide, MRs, bugs, spec misalignments. Running in CI is a separate issue.
- Output is a report and a patch. The developer reviews the patch, applies or edits it, and opens the MR themselves.
- Nothing leaves the container except files in `~/results`.

## Image

- One image: the OCUDU build toolchain (for `compile_commands.json`), the formatters and checkers CI runs, and
  include-what-you-use.
- Repos are not baked in. Each is bind-mounted read-only when `SRC_<REPO>` is set, else cloned on start.
- ocudu_tools is mounted by default from the checkout that holds the box, since the box's skills live there. Cloning
  it is the exception: `SRC_OCUDU_TOOLS=` (empty) in `.env`.
- Cloned ocudu is a partial clone (`--filter=blob:none`): history for diffs against a base, blobs fetched on demand.
  The reference repos are shallow clones.
- The container user has the host uid/gid, so bind mounts and results are owned by the developer.
- ccache is shared with the host, so targets already built there are warm in the box.

## Claude configuration

- The box's rules (`image/CLAUDE.md`) and permissions (`image/settings.json`) are user-level config, installed into
  `~/.claude/` on every start, so a new image replaces what an older one left in the persistent volume.
- Claude starts in the workspace, `~/work/ocudu`, so ocudu's own project instructions load on top of the box's rules.
  The box is not a project of its own.
- Skills are linked into `~/.claude/skills` from the repos, as listed in `image/skills.txt`. Review skills live in
  ocudu_tools' `skills/`, not in the box.
- Credentials: a `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY` is preferred, since it
  never refreshes. Without one, the host login is copied in, and replaced whenever the host's copy is newer; the host
  and the boxes then share one refresh token.
- The host's Claude config is mounted read-only and copied; the host is never written to.

## Workspace

- The developer's checkout is never written to. The review edits `~/work/ocudu`, a repo that borrows the checkout's
  objects through git alternates (instant, no copy).
- The workspace reproduces what the developer has: their HEAD plus uncommitted and untracked files, committed as one
  extra commit. With `--ref`, it is that ref instead, without the uncommitted work.
- That state is recorded as `refs/review/baseline`; the patch is the diff against it, so it holds only the review's
  changes.
- When the source is a partial clone, the workspace copies its promisor config, so missing blobs are fetched from
  origin.
- The workspace is made by the entrypoint on every start, not left as a step to remember. It and the build tree are
  container-local and lost with the container; `~/results` persists on the host.

## Unattended reviews

- `review-box.sh review --mr <iid>` runs `claude -p` with a prompt from `prompts/`, so each kind of review is a
  versioned prompt rather than a long command line. `{MR_URL}` and `{BASE}` are filled in by the launcher.
- Permissions are an allow-list (`image/headless-settings.json`), passed with `--settings`: nobody can approve a tool,
  so anything not on the list is refused. The deny rules still apply on top. Skipping permissions is not used.
- The workspace is the MR head, fetched from origin inside the box (`review-workspace init --mr`), so the developer's
  checkout needs no fetch and is never written to. The base is the MR's target branch.
- Claude's final message is the report. The launcher saves it in `results/mr-<iid>/` and, with `--post`, posts it on
  the MR from the host as a `Claude says:` quote. The box keeps its read-only GitLab token.

## Scope of a review

- One definition of scope for every skill: `diff [BASE]` (since `merge-base(BASE, HEAD)`, plus uncommitted), `path
  <dir>...` or `all`.
- Diff scope reviews the changed code and what it affects, not the untouched rest of each file.
- `path` arguments are relative to where the review was started.

## No writes outside the box

- git push is disabled through `url.push-disabled://.pushInsteadOf` and a remote helper that refuses it. Unlike a
  pre-push hook, `--no-verify` does not skip it. Fetch and clone still work.
- `GITLAB_TOKEN` must have only the `read_api` scope. It is the box's only GitLab credential, and its scope is what
  stops writes: a deny list cannot cover every way to reach the API.
- Claude is also denied `git push`, changes to the push rewrite, and the glab commands that write. These give an early,
  clear refusal; they are not the guarantee.
- Claude runs in its normal permission mode; relaxing it is a team decision. The read-only mounts keep the checkout
  safe either way.

## Guidelines review (`ocudu-guidelines-review`)

- The code guide in ocudu_docs is the rulebook; the skill holds no rules of its own. Every rule applies to every scoped
  file, and every violation is fixed except a closed list of report-only cases.
- Steps that need no judgement are scripts; the agent does not redo them by hand.
- It reviews the repo it is run from. The code guide, build tree and results dir come from env vars, which the box
  sets.
- Hex literals are lowercased with CI's expression, over whole files (CI enforces the whole tree), skipping `bundled/`
  and `external/` as CI does.
- iwyu only reports; its suggestions are applied through the guide's include rules. A header is checked through the
  source that implements it, found by resolving includes as the compiler does.
- Validation formats the edited files (diff scope: only changed lines), builds every source that includes an edited
  header directly or not, and runs the test executables built from or linking an edited target.
- The baseline is a ref under `refs/review/`, deleted once the patch is written.
