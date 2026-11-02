Review GitLab MR {MR_URL}. The current directory, ~/work/ocudu, is checked out at the MR head.

Use the ocudu-code-review skill on {MR_URL} with --base={BASE}.

You run unattended: nobody can answer a question or approve a tool. Do not ask; decide, and say in the report what you
decided. A tool that is refused stays refused; work without it.

The previous review of this MR is `{PREVIOUS_REPORT}` in `~/results` (`none` on the first review). If there is
one, read it before reviewing. Recheck each of its findings against the current head and report it under "Since the
last review": fixed, still open (with its new number), or no longer applies, and why. Never drop one silently.

Do not call ReportFindings. Write the review in the "MR comments" layout of `~/.claude/CLAUDE.md`: title line
first, findings as a numbered list, no tables.

Then post the report on the MR, once:

1. `review-results mr-{IID}` prints a new directory. Write the review to `comment.md` in it.
2. Run `review-post-note {IID} {PROJECT} <absolute path of comment.md>`.

Your final message is the report: markdown only, no preamble.
