---
name: open-pr
description: Open the pull request for a completed issue, with a Conventional Commit title and a body that links the spec and the evidence. Use as the final step of work-issue.
---

# Open the pull request

## Before opening

- `python scripts/check.py` is green.
- The tester reported PASS with real output.
- The reviewer has reviewed the diff and their findings are addressed or explicitly
  deferred with a reason.
- `git diff main...HEAD` contains no secret, mainnet address, extended public key, private
  IP or hostname.

## The title is not cosmetic

The pull request title becomes the squash commit subject, and `scripts/next_version.py`
reads that subject to mint the release version. So:

| Title prefix | Effect |
|---|---|
| `feat: ...` | minor version bump |
| `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:` | patch bump |
| `feat!: ...` or a `BREAKING CHANGE:` footer | major bump (minor while still `0.x`) |

Use the scope from the issue's area label where it helps: `feat(exchanges): ...`.

A non-conventional title does not fail the build — it silently mints the wrong version,
which is worse. Get it right.

## Steps

```bash
git push -u origin feature/<issue>-<slug>
gh pr create --title "<conventional title>" --body-file <body>
```

Body:

- what changed, in two or three sentences;
- a link to the spec, `docs/specs/NNN-<slug>.md`;
- the completed checklist from `.github/PULL_REQUEST_TEMPLATE.md`;
- the verification evidence: the criteria table and the gate output;
- `Closes #<N>`;
- the footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

Report the pull request URL back to the user.

## After opening

Do not merge. Only the repository owner merges, and CI has to be green first. If a required
check fails, fix it on the branch and push — do not close and reopen the pull request, and
do not merge around a red check.
