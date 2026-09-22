"""Every chain provider, imported here exactly once, on purpose.

**This package is empty of providers today.** Bitcoin is #7 and Kaspa is #8; this change
lands the seam and nothing that travels through it.

A provider module registers itself by decorating its class with
`@register_chain_provider(ChainKey.X)`, and a decorator only runs when its module is
imported. So each provider gets one line here and nothing else:

```python
from portfolio.providers.chains import bitcoin, kaspa  # noqa: F401
```

The import is explicit rather than discovered with `pkgutil.walk_packages`, and that is a
decision rather than an omission. Auto-discovery turns a provider that fails to import --
a syntax error, a missing dependency, a circular import -- into a chain that is simply
absent, and the symptom surfaces far from the cause: a balance that reads zero, or a 404
from an endpoint three layers up, noticed by whoever is looking at the portfolio rather
than by CI. An explicit import fails at startup, with a traceback pointing at the module
that broke.

The cost is a line somebody has to remember, so it is not left to memory: a test scans
this directory and asserts every module in it is registered, which means adding a file
without wiring it fails the build. That test is paired with a pinned count, so it cannot
pass merely because the directory is still empty.

Nothing is re-exported from here. A caller asks the registry for a provider by chain key;
it does not import a provider class by name, which is what keeps the set of chains a
runtime fact rather than something every call site has to know.
"""
