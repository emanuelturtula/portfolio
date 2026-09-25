"""Exchange providers: spot fills from the venues the owner trades on.

A sibling of `providers/chains/` and `providers/prices/`, and different from both in the
one way that shapes everything here: an exchange call is **signed with the owner's
credentials**, and its failures mean very different things -- a revoked key, a key without
read permission, a throttle, an outage, a window older than the venue keeps. So this package
fixes one vocabulary for all of them, and each venue is written against it:

| Module | Holds |
|---|---|
| `base` | the `ExchangeProvider` protocol, the fill and page types, the pure helpers |
| `errors` | the seven-class taxonomy, the error map, and `exchange_error` |
| `signing` | HMAC-SHA256, hex and Base64, over a `SecretStr` |
| `credentials` | `Credentials`, which cannot render a secret |
| `bitget` | Bitget spot fills over the Classic v2 API (#13) |
| `registry` | `exchange_providers`: the venues this process has credentials for |

**Bitget is the one venue so far.** BingX arrives with #14, with its endpoint paths, cursor
parameters, error codes and retention confirmed against its own documentation, and a line
in `registry`. #15 drives the providers. See `docs/providers.md`, "Exchange providers".

Nothing is re-exported from here: a caller imports from the module that defines a name, so
there is one spelling of each import.
"""
