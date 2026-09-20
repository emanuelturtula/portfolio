<!-- The title must be a Conventional Commit: the squash subject is what mints the version. -->

## What changes

<!-- Short summary. Link the spec if there is one: docs/specs/NNN-<slug>.md -->

## Type

- [ ] feat
- [ ] fix
- [ ] refactor / chore / docs / ci / build
- [ ] breaking change (`!`)

## Checklist

- [ ] `python scripts/check.py` is green
- [ ] No secrets, wallet addresses, private IPs or hostnames in the diff
- [ ] Test fixtures use testnet addresses only
- [ ] Money stays `Decimal` in Python and a string over the wire; no `float`
- [ ] Business logic is in a service, not a router
- [ ] Coverage thresholds were not lowered (if they were, justify it here)
- [ ] Everything in English: code, comments, docs, commits and this description

Closes #
