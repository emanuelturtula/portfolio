# 033 — The documentation, brought up to what was built

Issue: #26
Status: done

## Problem

The documents were written issue by issue, alongside the code each issue added. Three
things follow from that:

- **They are deep where an issue went deep, and silent where none did.** `docs/architecture.md`
  covers money, layering and authentication, but says nothing about the provider
  abstractions, which every chain, price source and exchange is built on.
- **The README still describes the skeleton.** It says "Status: early" and lists V1 as
  planned, with no screenshot.
- **Nobody has read each document whole, against the code, since it was first written.**
  A sentence that was true when its issue merged can stop being true when a later issue
  changes the code it describes.

## Scope

The issue's criteria, each followed by what it means here.

1. **`README.md` describes what the application actually does, with a real screenshot.**
   - What V1 does today; how to run it; where each document is.
   - The screenshot is of the real application, taken from a local build on a scratch
     database: test-network wallets, and exchange history and adjustments made up for the
     picture. The caption says so. Nothing in it is the owner's data (R2).
2. **`docs/architecture.md` covers the layering, the money representation decision and the
   provider abstractions.** Layering and money are there today. The missing section covers:
   - the three provider families (chains, prices, exchanges);
   - the protocol each implements;
   - the shared HTTP client and rate limiter they all go through;
   - capability declarations;
   - who calls a provider and when (the schedulers and the services);
   - the extended-key scanner.

   It points to `docs/providers.md` for the detail instead of repeating it.
3. **`docs/accounting.md` is complete with worked examples.** The eleven worked examples are
   already executed by `backend/tests/domain/accounting/test_worked_examples.py`.
   - Read the document against the engine, the reconciliation and the adjustments as they
     are now.
   - Add an example only where a behaviour the engine has is not illustrated. Every new
     example goes under `## Worked examples`, so that the existing test runs it.
4. **`docs/providers.md` records, per provider:**
   - the endpoints used;
   - the rate limits and retention windows actually confirmed;
   - explicitly, what remains unverified.

   Each provider gets a summary table at the top of its section. Every vendor fact in the
   table is one of three things:
   - *confirmed*, saying how and when;
   - *measured*, against the live service, saying when;
   - *unverified*, saying what would confirm it.

   The detailed sections stay where they are.

   Providers: Bitcoin Esplora (mempool.space, blockstream.info), the Kaspa REST API, each
   price source, Bitget and BingX.
5. **`docs/deployment.md` matches the current pipeline.** Read it against:
   - `.github/workflows/delivery.yml`, `ci.yml` and `remote-deploy.yml`;
   - `deploy/deploy.py` and `deploy/compose.yml`;
   - `scripts/next_version.py`.

   That includes the 30-minute backend job bound (#131) and the removal of the legacy
   deployment (#25, spec 032).
6. **No hostnames, IP addresses, real addresses or personal identifiers anywhere in the
   docs.**
   - This covers every file under `docs/` plus `README.md`, specs included.
   - Addresses in examples are test-network ones or written placeholders.
7. **Every document is in English.**

## Rulings

- **R1. The code is the source of truth.**
  - When a document and the code disagree, the document changes.
  - If the code looks wrong, the writer does not fix it here. They report it, and the tech
    lead files an issue.
- **R2. The screenshot shows no data of the owner's.**
  - The database is a scratch copy holding only test-network wallets and invented
    exchange history.
  - The image is checked for addresses, keys and amounts before it is committed.
  - Its file is a PNG under `docs/images/`.
- **R3. Specs are records, not documentation to keep current.**
  - A spec in `docs/specs/` describes what an issue decided when it merged, and is not
    rewritten here.
  - It is only scanned for criterion 6.
- **R4. `docs/operations.md` is not rewritten.** It is not among the criteria. Two open
  pull requests from other sessions touch it. It is scanned for criteria 6 and 7, and
  anything wrong in it is reported, not edited.
- **R5. Sentences pinned by documentation tests.** Many tests read a document and assert a
  sentence in it. A writer who changes a pinned sentence:
  - updates that test in the same change, and only to follow the new wording, never to
    weaken what it checks;
  - names every such test in their report.

## File ownership

| Agent | Owns |
|---|---|
| tech lead | `README.md`, `docs/images/**`, this spec |
| writer-architecture | `docs/architecture.md`, and the tests that read it (`backend/tests/security/test_no_float.py`, for its pinned sentences only) |
| writer-accounting | `docs/accounting.md`, `docs/adr/0001-weighted-average-cost-basis.md`, and the documentation tests that read `accounting.md` |
| writer-providers | `docs/providers.md`, and the documentation tests that read it |
| writer-deployment | `docs/deployment.md`, and the documentation tests that read it, including `tests/deploy/test_deploy_docs.py` |
| reviewer | nothing; reads every document against the code |
| tester | runs the full gate on the final tree |

A test that reads two of these documents, for example `test_reconciliation_documentation.py`,
which reads `accounting.md` and `providers.md`, can only be edited by one writer at a time.
The writer who needs it asks the tech lead first.

## Acceptance

All seven criteria hold, with three checks on top:
- the full gate passes;
- the reviewer finds no claim a document makes that the code contradicts;
- a scan of `README.md` and `docs/**` finds no hostname, IP address, mainnet address,
  extended key or personal identifier.

## What was done

- **`README.md`**: rewritten around what V1 does, with a screenshot of a local build on a
  demo database (R2) and an index of the documents.
- **`docs/architecture.md`**: a new Providers section covering the three families, their
  protocols and capabilities, the shared client and rate limiter, the extended-key scanner
  and who calls a provider when. Eight claims the code contradicted were corrected,
  including the layer order (`db -> config -> domain`).
- **`docs/accounting.md`** and the ADR: twelve claims corrected. Two new worked examples:
  a swap from units of unknown cost, and rebates. The existing test executes both.
- **`docs/providers.md`**: an at-a-glance table per provider, each fact marked confirmed,
  measured or unverified. Fifteen claims were corrected.
- **`docs/deployment.md`**: rewritten against the workflows and `deploy/deploy.py`. It
  includes the version rules, every job's time bound, and the fact that neither rollback
  path undoes a migration.
- **Criterion 6 in the specs**: one example in spec 018 named the repository owner's
  handle, and it is now a placeholder. The BingX probe account is still described as
  spec 017 decided, with no figures beyond "a few dozen fills in one symbol".
- **Stale docstrings and comments in code**, corrected because they are documentation too.
  All are comment or docstring changes, with no code change:
  - `services/accounting.py` and `api/routers/accounting.py`: the recompute triggers;
  - `db/types.py`: where the base-unit exponent comes from;
  - `services/scheduler.py`: four timers, not two;
  - `providers/exchanges/base.py`: `rate_limit` is read by nothing;
  - `config.py` and `providers/http.py`: no provider calls `strip_query`;
  - `.importlinter`, `deploy/compose.yml` and `.github/workflows/remote-deploy.yml`.

## Found, and left for their own issues

R1 kept these out of this change:

1. **A rollback cannot undo a migration.**
   - The application migrates its database forward at startup.
   - If a candidate migrates and then fails its health check, the previous image cannot
     start against the migrated database (`Can't locate revision`). The rollback then
     reports `rollback=failed`.
   - Reverting a change that added a migration fails the same way.
   - `docs/deployment.md` now says so.
2. **No import-linter contract keeps `fastapi` and `starlette` out of
   `portfolio.providers`.** It holds by convention only.
3. **`ExchangeCapabilities.rate_limit` is declared and validated, but nothing reads it.**
   Either wire it into the sync's pacing or remove it.
4. **`ChainProvider.health()` has no production caller.** Failover therefore hides a
   misconfigured instance everywhere but the logs.
5. **Bitget drops `Retry-After` on a 200 response.** Bitget's `_get` reads it only on a
   non-200 response, the same defect #115 records for BingX.
6. **Two open issues may already be resolved by #23.** #49 and #62 need checking.
7. **`CLAUDE.md` has drifted from the code.**
   - Rule 4's diagram has no `config` layer.
   - Rule 3 says the HTTP client logs URLs with the query removed. It logs no path or query
     at all.
8. **The tailnet join can use up the deploy job's safety margin.** Its `ping` can wait up
   to three minutes, which is the whole margin reserved around the 17-minute SSH call.
   Low risk.
