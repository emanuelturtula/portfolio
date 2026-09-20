---
name: implement-task
description: Implement one task from a spec using a test-first loop, respecting the project's money, layering and secret-handling rules. Use when writing implementation code for an issue.
---

# Implement a task

## The loop

1. Read the spec in `docs/specs/` and find your assigned acceptance criteria and file
   ownership. Touch only the files you own.
2. Write the failing test first. Run it; confirm it fails for the reason you expect. A test
   that passes before the implementation exists is testing nothing.
3. Write the smallest implementation that passes it.
4. `python scripts/check.py --backend` (or `--frontend`). Fix what it reports.
5. Commit with a Conventional Commit message scoped to what changed.
6. Repeat for the next criterion.

## Before you write a line

Read `CLAUDE.md`. The rules below are the ones that get violated most often and cost the
most to unwind.

**Money.** `Decimal` in Python, `TEXT` in SQLite via `NumericText`, integer base units for
on-chain quantities, JSON **string** over the wire, `string` in TypeScript. No `float`, no
`sqlalchemy.Numeric`, no `SUM()` over a money column, no `parseFloat`.

**Layering.** `api.routers -> services -> {repositories, providers} -> {db, domain}`, and
`domain` imports nothing. Routers parse, call a service, serialize.

**Secrets.** Environment variables into `SecretStr`. Never persisted, never returned by an
endpoint, never logged. Never log a full URL for an exchange call — one of them signs
requests in the query string.

**Time.** Timezone-aware UTC everywhere. Pass the clock into `domain` as an argument.

**Fixtures.** Testnet addresses only: `tb1`, `bcrt1`, `kaspatest:`, `tpub`.

## Things that are not yours to decide

- Do not lower a coverage threshold. If coverage drops, add the missing test.
- Do not add a blanket lint ignore. Narrow it to the exact line and explain why in a
  comment.
- Do not widen the scope beyond the spec. If you find an unrelated problem, note it for the
  tech lead to file as its own issue.
- Do not invent an external API endpoint. If the documentation does not confirm it, stop
  and verify, and record what you confirmed in `docs/providers.md`.

## When you are done

Report which acceptance criteria you implemented, which files you touched, and paste the
real output of the check command. If something does not pass, say so — a truthful failure
is worth more than a confident false claim, because the next agent builds on what you say.
