"""Exchange providers: spot fills from the venues the owner trades on.

A sibling of `providers/chains/` and `providers/prices/`, and different from both in the
one way that shapes everything here: an exchange call is **signed with the owner's
credentials**, and its failures mean very different things -- a revoked key, a key without
read permission, a throttle, an outage, a window older than the venue keeps. So before any
venue exists, this package fixes one vocabulary for all of them:

| Module | Holds |
|---|---|
| `base` | the `ExchangeProvider` protocol, the fill and page types, the pure helpers |
| `errors` | the seven-class taxonomy, the error map, and `exchange_error` |
| `signing` | HMAC-SHA256, hex and Base64, over a `SecretStr` |
| `credentials` | `Credentials`, which cannot render a secret |

**No venue is implemented yet, and nothing registers one.** Bitget arrives with #13 and
BingX with #14, each with its endpoint paths, cursor parameters, error codes and retention
confirmed against its own documentation; the registry arrives with the first of them,
because its factory needs `Credentials` and settings only a real venue introduces. #15
drives the providers. See `docs/providers.md`, "Exchange providers".

Nothing is re-exported from here: a caller imports from the module that defines a name, so
there is one spelling of each import.
"""
