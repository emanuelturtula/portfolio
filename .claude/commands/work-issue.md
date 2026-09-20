---
description: Take a GitHub issue from its number to an open pull request using an agent team
argument-hint: <issue-number>
---

Work GitHub issue **#$ARGUMENTS** from start to an open pull request.

You are the tech lead. Read `.claude/agents/tech-lead.md` and `CLAUDE.md` before doing
anything else, then run this sequence without stopping to ask for approval — the user
reviews the result on the pull request, not mid-flight.

## 1. Read the issue

```bash
gh issue view $ARGUMENTS --json number,title,body,labels,milestone
```

If the issue does not exist, or is already closed, or already has an open pull request
linked, stop and say so instead of working it.

## 2. Branch

```bash
git switch -c feature/$ARGUMENTS-<short-slug> main
```

The slug comes from the issue title: lowercase, hyphenated, three or four words.

## 3. Spec

Use the `write-spec` skill. Write `docs/specs/NNN-<slug>.md` and commit it on its own.

## 4. Assemble the team

Spawn teammates with the Agent tool, one per role, using the definitions in
`.claude/agents/`. Pick from the issue's labels:

| Label | Teammate |
|---|---|
| `area:backend`, `area:providers`, `area:accounting` | `backend-dev` |
| `area:frontend` | `frontend-dev` |
| always | `tester` |
| always | `reviewer` |

Do not spawn a teammate the issue does not need — an idle agent still costs tokens and adds
coordination noise.

Give each one: the issue number, the spec path, **their exact file ownership from the spec**,
and the acceptance criteria they are responsible for. File ownership must be disjoint; two
agents editing one file overwrite each other's work and you will not notice until the tests
contradict themselves.

## 5. Implement

The implementers use the `implement-task` skill. Let them work. Answer their questions,
resolve conflicts between them, and keep them inside the spec's scope.

## 6. Verify

The tester uses the `verify-task` skill. If the report is FAIL, hand it back to the
implementer with the specific failing criterion. Do not proceed on a FAIL, and do not
accept a PASS that has no command output in it.

## 7. Review

The reviewer reads `git diff main...HEAD`. Address what they find, or record explicitly why
a finding is being deferred.

## 8. Open the pull request

Use the `open-pr` skill. Report the URL back to the user.

## Throughout

- Everything in English.
- Nothing sensitive in the diff: no real wallet address, no API key, no private IP, no
  hostname.
- `python scripts/check.py` must be green before the pull request opens.
- You do not merge. Only the repository owner merges.
