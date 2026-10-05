# 032 — Decommission the legacy deployment

Issue: #25
Status: done. The host-side steps ran on 2026-10-04; the image package is the owner's to remove.

## Problem

The application this repository replaced still ran on the same host: a production and a
test container, published on ports 80 and 8080. It held its own copy of the owner's trade
history, synced from the exchanges by its own code. Two copies of one history, maintained
separately, drift apart, and only one of them is the one the owner reads.

The issue's condition was explicit. The legacy application stays running until its data
is either migrated or confirmed unnecessary.

## Owner's rulings (2026-10-04)

- **R1. The legacy data is not needed in V1.** The owner ruled after a read-only comparison
  of the two databases, made inside each container, which printed only aggregates and
  differences:
  - V1 holds every exchange trade the legacy database holds, with the same base quantities
    and quote amounts.
  - The differences were limited to three things: fee detail where V1's historical import
    recorded a zero fee, how fills are grouped, and one execution the legacy database had
    stored twice.
  - The legacy database also holds what V1 never set out to keep: on-chain transaction
    history and price and balance snapshot history.

  None of this is deleted. It stays in the legacy data volume and in the off-host copy.
- **R2. The off-host copy lives on the owner's workstation, outside any repository.** The
  issue asks for a copy off the host, unlike the V1 backups of spec 029, which stay on the
  Pi by the owner's ruling for #22.
- **R3. The legacy repository no longer exists on GitHub,** so there is nothing to archive.
- **R4. The legacy container image package is the owner's to remove.**
  - Deleting a package is irreversible once GitHub's 30-day restore window has passed, so
    the owner does it.
  - The agent's token has no package scope, so it could not anyway.
  - The local copy of the image on the host is kept regardless (see *Undoing it*).

## What was done

All of it ran over SSH, with the owner's explicit allowance in chat.

1. **Inventory**, read-only:
   - the legacy containers, their compose project labels, their restart policy
     (`unless-stopped`), their volumes and their ports;
   - no cron entry or systemd unit that would start them again;
   - no remaining pipeline that could redeploy them, since the repository is gone.
2. **Copy.**
   - The sqlite3 backup API, run inside the legacy production container, made a
     consistent copy of the live database while it was in use. The copy went to the
     container's own `/tmp`, never to the data volume.
   - It was made standalone with `PRAGMA journal_mode=DELETE`, the step spec 029 found a
     backup needs.
   - On the host, `PRAGMA integrity_check` returned `ok`, and every table's row count
     matched the live database.
3. **Off-host copy and its verification**:
   - copied to the workstation and checked against a `sha256` taken on the host;
   - opened there read-only and `immutable`;
   - `integrity_check` was `ok` again, the row counts matched again, and no sidecar file
     was left behind;
   - a `.sha256` file sits beside it.

   The transit copy on the host was then removed. The data volume itself was not touched.
4. **Removal.** Both legacy containers were stopped and removed, along with the two compose
   networks only they used.
5. **Ports.** Nothing listens on 80 or 8080. V1 on 8083 and the other services on the host
   are unaffected, and V1's `/api/health` answers 200.

## Undoing it

Everything needed to recreate the containers is still on the host:
- both data volumes;
- the image, by digest;
- the legacy deploy root, which holds the compose file each container was created from.

Removing those is a separate decision for the owner. The legacy deploy root also still holds
the legacy application's `secrets.env` files. Any exchange API key that only the legacy
application used can now be revoked at the exchange.

## Acceptance criteria

| Criterion | State |
|---|---|
| The legacy data volume is backed up off the host first, and the backup is verified by opening it | Done (steps 2-3) |
| The legacy production and test containers are stopped and removed | Done (step 4) |
| Their ports are confirmed free | Done (step 5) |
| The legacy repository is archived on GitHub | Moot: it no longer exists (R3) |
| The legacy container image package is deleted or marked deprecated | The owner's action (R4) |
| `docs/deployment.md` no longer references the legacy deployment | Done in this change |
