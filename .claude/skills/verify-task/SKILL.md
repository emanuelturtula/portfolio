---
name: verify-task
description: Verify an implementation against its spec's acceptance criteria and report PASS or FAIL with real command output as evidence. Use before opening a pull request.
---

# Verify a task

Verification means proving each acceptance criterion with a test and a command you actually
ran. It does not mean reading the code and forming an opinion.

## Steps

1. Read the spec's acceptance criteria and its test plan table.
2. For each criterion, find the test that proves it. Run that test **by name** and keep the
   output. If no test proves it, the criterion FAILS — regardless of whether the feature
   appears to work.
3. Run the full gate: `python scripts/check.py`.
4. Check the security invariants specifically, because they are the ones a passing test
   suite does not automatically cover:
   - no mainnet address, extended public key, API key, private IP or hostname in the diff
     (`git diff main...HEAD`);
   - fixtures use testnet prefixes only;
   - no `float`, `sqlalchemy.Numeric`, `SUM()` over money, or `parseFloat` on money;
   - no endpoint returns a credential.
5. Write the report.

## Report format

```markdown
## Verification: #<N> <title>

**Result: PASS** (or FAIL)

| # | Criterion | Test | Result |
|---|---|---|---|
| 1 | ... | `tests/...::test_...` | PASS |

### Gate output

<paste the real output of scripts/check.py here, not a summary of it>

### Security checks

- Secrets in diff: none / <what was found>
- Fixture addresses: testnet only / <what was found>
- Money handling: no float / <what was found>

### Findings

<anything that passed but concerns you, or nothing>
```

## The rules of this role

- **Never report PASS for a command you did not run.** Paste the output. If you find
  yourself writing "should pass" or "presumably passes", the answer is FAIL.
- **Partial is FAIL.** Six of seven criteria met is not a pass; say which one is missing.
- Do not fix the implementation. Report the failure and hand it back.
- Do not lower a threshold, skip a test or add an ignore to turn a FAIL into a PASS. That
  is the one thing that makes this whole role worthless.
