---
name: write-spec
description: Turn a GitHub issue into a committed implementation spec under docs/specs/. Use when starting work on an issue, before any code is written.
---

# Write a spec from an issue

The spec is the contract between the issue and the pull request. It exists so that the
implementers do not each interpret the issue differently, and so the reviewer has something
concrete to check the diff against.

Keep it short. A spec nobody reads is worse than no spec.

## Steps

1. `gh issue view <N> --json number,title,body,labels` — read it completely.
2. Read the existing code in the areas the issue touches. Do not design against an imagined
   codebase. If a pattern already exists, follow it; if you are deliberately departing from
   one, say why in the spec.
3. Check `docs/specs/` for the highest existing number and use the next one.
4. Write `docs/specs/NNN-<slug>.md` using the template below.
5. Commit it on its own: `docs: add spec for #<N> <short title>`.

## Template

```markdown
# NNN — <Title>

Issue: #<N>
Status: draft | implementing | done

## Problem

What is not possible today, in two or three sentences. Not a restatement of the title.

## Scope

What this change includes.

## Non-goals

What it deliberately does not include, and where that work belongs instead. This section
prevents scope creep more effectively than any other part of the spec.

## Design

The approach, and the alternatives you rejected with one line each on why. Include the
module and file paths you will create or change.

## API contract

New or changed endpoints: method, path, request shape, response shape, error cases.
Monetary fields are JSON strings — mark them.

## Data model

New tables and columns, constraints, indexes, and the Alembic migration. Note explicitly
whether the migration is reversible.

## Acceptance criteria

Copy them verbatim from the issue, numbered. Each one gets a test in the next section.
If the issue's criteria are ambiguous, write your interpretation here — do not silently
pick one.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | ... | `backend/tests/.../test_x.py::test_y` |

Include the failure cases, not just the happy path.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/...` |
| frontend-dev | `frontend/src/...` |
| tester | `backend/tests/...`, `frontend/src/**/*.test.tsx` |

Paths must be disjoint. Two agents editing one file overwrite each other.

## Risks

What might be wrong about this plan, and anything that depends on external API behaviour
that has not been confirmed against live documentation.
```

## Rules

- English only.
- No secrets, wallet addresses, private IPs or hostnames in the spec.
- If the issue is under-specified, write your interpretation in the acceptance criteria
  section and flag it in Risks. Do not stall, and do not guess silently.
- If the issue is genuinely too large for one pull request, say so in a comment on the
  issue and propose a split before writing the spec.
