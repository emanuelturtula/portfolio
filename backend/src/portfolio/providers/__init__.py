"""Outbound adapters for exchanges, wallets and price feeds.

The chain side of this package is a protocol and a registry rather than a base class:
`base.py` says what a chain provider must offer, `registry.py` says which one answers for
which chain, `http.py` is the shared client they all make their calls through, and
`chains/` holds one module per chain. `docs/providers.md` is the contract a new chain
implements.

`float` is banned in this package, as it is in `domain/` and `services/`, and an AST test
enforces it. On-chain quantities are integer base units and durations are integer
milliseconds; `http.py` explains why the second is not an evasion of the first.
"""
