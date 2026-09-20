# Portfolio

A self-hosted crypto investment portfolio tracker. It reads balances from on-chain
addresses, imports spot trade executions from exchanges, and answers three questions:

- what is the portfolio worth right now,
- what is each wallet worth,
- how much has actually been invested in each asset.

Single user, single production instance, running on a Raspberry Pi on a home network.

> **Status: early.** The delivery pipeline and the application skeleton are in place. The
> features below are tracked in the [milestones](../../milestones) and are being built
> issue by issue.

## Planned for V1

| Area | Scope |
|---|---|
| On-chain balances | Bitcoin and Kaspa, from addresses held in a hardware wallet, behind a provider layer that any other chain can plug into |
| Exchanges | Bitget and BingX, read-only import of spot fills |
| Accounting | Weighted-average cost basis per asset, reconciled against observed on-chain balances |
| Dashboard | Total value, value per wallet, amount invested and unrealized profit and loss per asset |

Tangem has no API: it is a hardware wallet, and what it provides is the public address. The
application reads those addresses from public block explorers. Exchange keys are read-only.

## Stack

Python 3.12 + FastAPI, React + TypeScript + Vite, SQLite, one Docker image serving both the
API and the single-page application.

## Running it locally

```bash
cd backend && uv sync && uv run uvicorn portfolio.main:create_app --factory --reload
```

```bash
cd frontend && npm install && npm run dev
```

The frontend dev server proxies `/api` to the backend on port 8000.

```bash
python scripts/check.py        # lint, types, layering, tests, coverage, secret scan
```

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
for the agents and for me is in [CLAUDE.md](CLAUDE.md).

## License

MIT
