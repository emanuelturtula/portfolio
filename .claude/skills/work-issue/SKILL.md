---
name: work-issue
description: Take a GitHub issue from its number to an open pull request in this session - branch, spec, test-first implementation, the full gate, a review of the diff, and the pull request.
argument-hint: <issue-number>
---

# Work an issue

Take GitHub issue **#$ARGUMENTS** to an open pull request, in this session and without
subagents. Run straight through: the owner reviews the result on the pull request, not
mid-flight. Stop to ask only when the issue is ambiguous in a way that changes what gets
built, and say what you would pick.

Read `CLAUDE.md` first. Its rules sit behind every acceptance criterion.

## 1. Read the issue

```bash
gh issue view $ARGUMENTS --json number,title,body,labels,milestone,state
```

If it does not exist, is closed, or already has an open pull request, stop and say so.
Read the acceptance criteria literally: they are the contract.

If it is too large for one pull request, say so in a comment on the issue and propose a split
before writing anything.

## 2. Branch

```bash
git fetch origin
git switch -c feature/$ARGUMENTS-<slug> origin/main
```

The slug is three or four hyphenated words from the title.

## 3. Spec, for a feature

For a `type:feat` issue, or any change that spans both sides or adds a table or an endpoint,
write `docs/specs/NNN-<slug>.md` with the next free number and commit it on its own as
`docs: add spec for #<N> <short title>`. For a small fix or chore, the issue is the spec.

Read the code the issue touches before designing. If a pattern exists, follow it; if you
depart from one, say why. Keep it short: a spec nobody reads is worse than none.

```markdown
# NNN — <Title>

Issue: #<N>

## Problem
What is not possible today, in two or three sentences.

## Scope / Non-goals
What this includes, and what it deliberately leaves out and where that belongs.

## Design
The approach, the files it creates or changes, and each rejected alternative in one line.

## API contract
Method, path, request and response shapes, error cases. Mark every money field as a string.

## Data model
Tables, columns, constraints, indexes, the migration, and whether it is reversible.

## Acceptance criteria
Verbatim from the issue, numbered. Where one is ambiguous, write your interpretation here.

## Test plan
| # | Criterion | Test |
|---|---|---|

## Risks
What might be wrong, and any vendor behaviour not yet confirmed against its documentation.
```

## 4. Implement, test first

For each criterion: write the failing test, run it and see it fail for the reason you expect,
write the smallest implementation that passes, then `python scripts/check.py --backend` (or
`--frontend`). Commit with a Conventional Commit message.

What good tests look like here:

- **Failure paths, not only the happy one.** For a provider: 401, 403, 429 with
  `Retry-After`, 5xx, truncated JSON, a missing field, an extra field, an empty page, many
  pages, and a cursor that stops advancing.
- `domain/` tests are pure and fast. Invariants that must hold for any valid sequence of
  events get `hypothesis`.
- Repository tests use a file-backed SQLite under `tmp_path`, never `:memory:`, which
  vanishes between pooled async connections.
- Provider tests use `respx` against hand-written, sanitized fixtures. Signing helpers get
  golden vectors.
- Frontend views are tested in all four states, not only success.

Not yours to decide: lowering a coverage threshold, a blanket lint ignore (narrow it to the
line, with a comment saying why), skipping or deleting a failing test, widening the scope (an
unrelated problem becomes its own issue), or an endpoint the vendor's documentation does not
confirm.

## 5. Gate

```bash
python scripts/check.py
```

Green, in full. Never claim a result for a command you did not run.

## 6. Review the diff as an adversary

Read `git diff origin/main...HEAD` looking for, most severe first:

1. **Anything sensitive**: a real wallet address, an extended key, an API key, a private IP,
   a hostname, an infrastructure username. Fixtures above all: the likely accident is an
   address pasted from a vendor's example.
2. **Money as a float**: `float(`, a float literal, `sqlalchemy.Numeric`, `SUM()` or
   `ORDER BY` over a money column, a JSON number where a string belongs, `parseFloat` or
   `Number()` on money.
3. **A correctness bug with a concrete failing input**: pagination that can loop forever, a
   checkpoint committed before the data it covers, an off-by-one in a retention window, a
   naive datetime in an ordering key, an ordering that is not total.
4. **A layering violation**: logic in a router, `domain/` reaching for a clock or the network,
   a service importing `fastapi`.
5. **A criterion claimed but not proven** by a named test.
6. **An error path that swallows**: a bare `except`, a failure logged and then carried on
   with a zero, an authentication failure reported as "temporarily unavailable".

Fix what you find. For a large or risky change, also run `/code-review` for a pass with a
fresh context.

## 7. Open the pull request

The title becomes the squash subject, and `scripts/next_version.py` reads it to mint the
version. A wrong prefix does not fail the build; it ships the wrong version.

| Title prefix | Bump |
|---|---|
| `feat:` | minor |
| `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:` | patch |
| `feat!:` or a `BREAKING CHANGE:` footer | major (minor while `0.x`) |

Scope it from the area label where that helps: `feat(exchanges): ...`.

```bash
git push -u origin feature/$ARGUMENTS-<slug>
gh pr create --title "<conventional title>" --body-file <body>
```

The body: what changed in two or three sentences, the spec link if there is one, the
checklist from `.github/PULL_REQUEST_TEMPLATE.md`, a criterion-to-test table, the gate
result, and `Closes #$ARGUMENTS`. Report the URL.

CI must be green before a merge. If a check fails, fix it on the branch and push. Merge with
`gh pr merge --squash` when the owner asks; merging deploys to the Pi.
