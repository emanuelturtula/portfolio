"""Price sources: what an asset costs in a fiat currency, from four vendors.

A sibling of `providers/chains/`, and the difference between the two is the whole reason
this package exists as its own import target. A chain read answers a question about the
owner's addresses; a price read answers a question about the market, which is the same
answer for everyone. What they share is that both are vendor calls with latency and
outages attached, and **only this package may be reached from a request path** -- a rule
`backend/.importlinter` states as a contract with no `allow_indirect_imports`, so
`api.routers -> services -> providers.prices` is caught as a chain rather than only as a
direct import.

Nothing is auto-discovered. `base.price_sources` names the four classes in order, for the
reason `providers/registry.py` gives about `pkgutil.walk_packages`: a source that was
written but never wired should fail as something a reader can see, not as a price that
quietly never arrives.
"""
