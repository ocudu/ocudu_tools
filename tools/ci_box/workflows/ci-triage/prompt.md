Triage the pipeline {URL} with the ci-triage skill: run `/ci-triage {URL}` and follow it to the end.

This run is unattended. Nobody can answer a question or approve a tool: never ask. If a tool is refused, do not try
workarounds or probe the permissions: skip it if the skill can do without it, otherwise stop and say which call was
refused. Do not post or change anything on GitLab; the box does that from `triage.json` after you finish.

You are done when `build_triage.py` prints the table without problems. Your final message is that table, unchanged.
