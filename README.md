# OCUDU Tools

Collection of scripts, skills, other AI-related tools to use with OCUDU Project.

## Installation

### Claude Code (plugin marketplace)

Register this repo as a marketplace source:

```text
/plugin marketplace add git@gitlab.com:ocudu/ocudu_tools.git        # latest main
/plugin marketplace add git@gitlab.com:ocudu/ocudu_tools.git#1.0.0  # pin to a tag or branch
```

Now you can search for OCUDU skills inside Claude Code:

```text
/plugins
```

Or directly install **individual skills**:

```text
/plugin install ci-triage@ocudu_tools
/plugin install analyze-ran-log@ocudu_tools
/plugin install ocudu-guidelines-review@ocudu_tools
```

`ocudu-guidelines-review` reads the code guide from an `ocudu_docs` checkout next to your OCUDU checkout (or
`OCUDU_CODE_GUIDE`), and needs `include-what-you-use`, `clang-format-21` and `jq`. `tools/code_review_box` runs it in a
container that has all of that and leaves your checkout untouched.

To get updates after the repo changes:

```text
/plugin marketplace update ocudu_tools
```

You might need to run `/reload-plugins` after install / upgrade.

### Manual (symlink)

Clone the repo and run the install script. Symlinks stay in sync as you pull updates.

```bash
git clone git@gitlab.com:ocudu/ocudu_tools.git
cd ocudu_tools
./install.sh                  # installs to ~/.claude/skills/ by default
./install.sh /path/to/.claude # custom install target
```

You might need to restart Claude Code after adding skills.
