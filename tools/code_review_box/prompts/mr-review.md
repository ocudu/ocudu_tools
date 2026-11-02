Review GitLab MR {MR_URL}. The current directory, ~/work/ocudu, is checked out at the MR head.

Use the ocudu-code-review skill on {MR_URL} with --base={BASE}.

You run unattended: nobody can answer a question or approve a tool. Do not ask; decide, and say in the report what you
decided. A tool that is refused stays refused; work without it.

Do not call ReportFindings. Write the review in the "MR comments" layout of `~/.claude/CLAUDE.md`: title line
first, findings as a numbered list, no tables. Your final message is the report and is posted on the MR as it is:
markdown only, no preamble.
