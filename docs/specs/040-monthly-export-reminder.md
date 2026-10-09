# 040 — Monthly reminder to export the exchanges' transactions

Issue: none; the owner's request of 2026-10-09
Status: in progress. Pull request "feat: remind on the dashboard to export each exchange's transactions every month"

## Problem

The owner keeps each exchange's transaction history as the exchange's own CSV export, filed
month by month in their own storage. Nothing reads those exports automatically:

- Binance refuses API calls from cloud hosts in restricted locations;
- a personal Nexo account has no public API;
- spec 036 removed the Bitget and BingX integrations.

So the export is a manual chore, and a manual chore that nothing prompts is one that gets
skipped. The owner asked for a reminder on the dashboard, every month, that stays until they
mark it done.

## Scope

- **`GET /api/exports/reminder`** answers `{months, exchanges}`:
  - `months` is every closed month not marked done, oldest first, each as `YYYY-MM`;
  - `exchanges` is the names the reminder lists.
- **`POST /api/exports/months/{month}/done`** marks a month done and answers the same body.
  - Marking a month twice changes nothing.
  - A month that has not ended, or one before the first reminded month, is a `409`.
  - A `month` that is not `YYYY-MM` is a `422`.
- **One table, `export_months`**, with one row per owner and month marked done, created by
  migration `0015_export_months`.
- **The dashboard shows a reminder under the backup notice** while any month is owed:
  - one line per month, each with its own "Mark as done";
  - a line that says so when the check fails.

## Non-goals

- **Fetching, storing or reading the exports.** The application never sees the files.
- **Undoing a mark.** Nothing asks for it. A mistaken mark is one `DELETE` in SQLite.
- **One mark per exchange.** The owner asked to mark the month as finished.
- **Configuring the exchanges or the first month.** Both are constants in
  `domain/export_reminders.py`, and changing either is a one-line diff.

## Rulings

- **R1. A month is a calendar month in Argentina time**, the owner's: UTC-3 all year, with no
  daylight saving time since 2009. A month is owed from midnight on the next 1st, Argentina
  time (03:00 UTC). The offset is a constant because `domain` may not read the host's
  time-zone database.
- **R2. The first reminded month is 2026-09.** Earlier months are not owed. September is owed
  from the day this deploys, so the reminder shows at once and one click clears it.
- **R3. The exchanges are Binance, Bitget, BingX and Nexo**, listed in that order.
- **R4. Every owed month stays on screen.** A skipped month is not folded into the next one,
  because the export for each month is a separate file.
- **R5. A failed check is said, not hidden.** "Nothing owed" and "could not tell" call for
  different things from the owner (CLAUDE.md, the four states). Loading renders nothing, so
  the dashboard does not flash a box on every visit.
- **R6. The marks are per owner**, scoped by `user_id` like every other query.

## Acceptance criteria

1. Before the first reminded month ends, `GET /api/exports/reminder` answers no months.
2. From 03:00 UTC on the 1st, the month before is owed. One second earlier, it is not.
3. Marking a month removes it from `months`. Marking it again writes nothing.
4. A month that has not ended, or one before 2026-09, cannot be marked (`409`). Text that is
   not `YYYY-MM` is a `422`.
5. Both endpoints answer `401` without a session.
6. The dashboard shows each owed month with a "Mark as done" button. After a click the line
   goes away, and the reminder disappears once nothing is owed.
7. When the reminder cannot be read, the dashboard says so in a `role="alert"` line.
