
## GitLab comments

This box was started with GitLab comments enabled. You may post MR notes, and nothing else: no approving, merging,
editing or closing MRs, no issues, no `glab api` writes. `git push` stays disabled.

- Post with `review-post-note <iid> <project> <file>`: write the note to a file under `~/results` first, in the "MR
  comments" layout. The command refuses a file whose first line is not the title line. `glab mr note` itself is denied.
- Post only what the task asks for. When in doubt, write it to `~/results` instead.
