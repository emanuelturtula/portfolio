# Portfolio

A self-hosted crypto portfolio tracker. It reads balances from Bitcoin and Kaspa wallets
on-chain and values them in USDT from cached prices, answering three questions:

- what is the portfolio worth right now,
- what is each wallet worth,
- what were they worth on each day before.

Single user, single production instance, running on a Raspberry Pi on a home network.

## What it does

| Area | What V1 does |
|---|---|
| On-chain balances | Bitcoin addresses, and single-signature `xpub`, `ypub` and `zpub` keys whose addresses are derived locally up to a gap limit of 20. Kaspa addresses. Read from public explorers on a schedule, Bitcoin with a fallback explorer, and every run logged. |
| Prices | Kraken, Coinbase, the Kaspa API and, with an optional key, CoinGecko, cached and refreshed on a schedule. Prices are kept in EUR and USD, each priced directly rather than converted from the other; the dashboard reads the USD price as USDT. A missing price shows as missing, never as zero. |
| Price history | One price per asset per UTC day: the day's close from Kraken's daily candles, backfilled once a day for the 720 days Kraken keeps, and the hourly price for today. |
| Dashboard | The total value and each holding in USDT, from the cached USD price read as USDT one for one, with each holding's share. A wallet that has not been read or whose reading is stale, and a price that is missing or stale, are named beside the figures rather than counted as zero. |
| Value over time | An area chart of the total value per day on the dashboard, over 30 days, 90 days, a year or everything, and a chart of each wallet's value on the Details page. A day that cannot be valued, because no price or no reading exists for it, is a gap in the line, never a zero. |
| Operations | Scheduled SQLite backups with daily and weekly retention, and a restore command. A health page that reports every source. One log line per record, a correlation id per request, and credentials and extended keys redacted from every line. |

The pages are Dashboard, Details, Wallets, and Health at `/health`.

No key that can spend is ever given to it. A wallet is a public address or an extended
public key, and a value that looks like a private key is refused before it is stored.

Exchange sync (Bitget, BingX), cost-basis accounting, manual adjustments and the holdings
check were removed in spec 036; `docs/operations.md`, section 12, says what an operator does
about it.

## Running it locally

```bash
cd backend && uv sync && uv run uvicorn portfolio.main:create_app --factory --reload
```

```bash
cd frontend && npm install && npm run dev
```

The frontend dev server proxies `/api` to the backend on port 8000. The backend creates
`backend/data/portfolio.db` on first start and migrates it to the latest schema on every
start.

There is one account. The simplest way to create it is to start the backend once with
`PORTFOLIO_BOOTSTRAP_PASSWORD` set: the first start creates the account `owner` with that
password, and later starts ignore the variable. The other way is the command line:

```bash
cd backend && uv run python -m portfolio create-user
```

Every setting is a `PORTFOLIO_`-prefixed environment variable. They are listed with their
defaults in `backend/src/portfolio/config.py`, and explained where they matter in
[docs/operations.md](docs/operations.md). The defaults read public explorers and price
sources, so the only thing it needs to be given is the account's password, and optionally a
CoinGecko key.

```bash
python scripts/check.py        # lint, types, layering, tests, coverage, secret scan
```

## Documentation

| Document | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The layering, how money is represented, authentication, and the provider abstractions every chain and price source is built on |
| [docs/providers.md](docs/providers.md) | Every external service: the endpoints used, the limits, and which facts are confirmed and which are not |
| [docs/deployment.md](docs/deployment.md) | The pipeline from a merge to a running container, the host layout, and the rollback |
| [docs/operations.md](docs/operations.md) | Running the instance: the account, each source, the schedules, backups, logs, the health detail, and troubleshooting |
| [docs/specs/](docs/specs/) | One spec per issue: what each change set out to do and what it decided. A record, not kept current |

## Stack

Python 3.12 + FastAPI, SQLAlchemy and Alembic on SQLite, React + TypeScript + Vite, one
Docker image serving both the API and the single-page application.

## A note on this repository being public

It is a financial application, so nothing sensitive is ever committed: no API keys, no
wallet addresses, no extended public keys, no infrastructure detail. Those are runtime user
data and live in the database on the host, or in environment variables.

That is enforced in four independent places rather than by discipline alone — a gitleaks
configuration with project-specific rules, a scanner that fails closed, pre-commit and
pre-push hooks, and a CI job that scans the full history. Test fixtures are required to use
testnet addresses, which is what makes the rule mechanically checkable.

## Deployment

Merging to `main` builds an arm64 image, pushes it to the registry by digest, deploys it
over Tailscale, verifies the container is healthy, and only then tags the image and cuts a
release. The host-side script backs up the database before replacing anything and rolls
back if the new container does not become healthy.

See [docs/deployment.md](docs/deployment.md).

## Contributing

This is a personal project and pull requests are not being accepted. The working agreement
for Claude Code and for me is in [CLAUDE.md](CLAUDE.md).

## License

MIT. See [LICENSE](LICENSE).
