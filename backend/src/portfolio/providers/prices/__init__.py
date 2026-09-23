"""Price sources: what an asset costs in a fiat currency, from four vendors.

A sibling of `providers/chains/`, and the difference between the two is the whole reason
this package exists as its own import target. A chain read answers a question about the
owner's addresses; a price read answers a question about the market, which is the same
answer for everyone. What they share is that both are vendor calls with latency and
outages attached, and **no request path may reach this package at all** -- a rule
`backend/.importlinter` states as a contract with no `allow_indirect_imports`, so
`api.routers -> services -> providers.prices` is caught as a chain rather than only as a
direct import.

That contract is why `services/prices.py` imports nothing from here and
`services/price_refresh.py` is the only module in `services/` that does. If the contract
sent you here, that split is the thing it was protecting: a request renders from the
`prices` table, and filling that table is somebody else's job on somebody else's schedule.

Nothing is auto-discovered. `registry.price_sources` names the four classes in order, for
the reason `providers/registry.py` gives about `pkgutil.walk_packages`: a source that was
written but never wired should fail as something a reader can see, not as a price that
quietly never arrives.
"""
