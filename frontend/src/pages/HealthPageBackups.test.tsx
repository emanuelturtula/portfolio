import { screen, waitFor, within } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { BackupStatus } from '@/api/health';
import { HealthPage } from '@/pages/HealthPage';
import {
  backupStatus,
  disabledBackup,
  failedBackup,
  failedBackupWithNoKind,
  HEALTH_DETAIL_PATH,
  NEWEST_BACKUP_AT,
  okBackup,
  pendingBackup,
  serveBackup,
  staleBackup,
  unreadableBackup,
} from '@/test/backupFixtures';
import { renderWithProviders, settle } from '@/test/render';
import { HEALTH_PATH, problem, server } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The Health page's "Backups" section (spec 029): the state in words, the newest copy, the
 * count, and why the last attempt failed. Read as the owner reads it -- each `<dt>` with the
 * `<dd>` that follows it -- so a value under the wrong label fails.
 */
async function backupsSection(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Backups' });
}

/** Every label in the section with its value, in order, whitespace made plain. */
function rows(section: HTMLElement): [string, string][] {
  const terms = Array.from(section.querySelectorAll('dt'));
  return terms.map((term) => [
    term.textContent,
    (term.nextElementSibling?.textContent ?? '').replace(/\s+/gu, ' '),
  ]);
}

function renderPage(backup: BackupStatus) {
  const served = serveBackup(backup);
  server.use(served.handler);
  renderWithProviders(<HealthPage />);
  return served;
}

async function answeredSection(backup: BackupStatus): Promise<HTMLElement> {
  renderPage(backup);
  const section = await backupsSection();
  await within(section).findByText('State');
  return section;
}

describe('HealthPage: the backups section', () => {
  it('shows the state, the newest copy and the count when backups are running', async () => {
    inTimeZone('UTC');

    const section = await answeredSection(okBackup);

    expect(rows(section)).toEqual([
      ['State', 'OK. Scheduled backups are running.'],
      ['Newest backup', 'Oct 2, 2026, 3:00 AM'],
      ['Backups kept', '9'],
    ]);
    expect(
      within(section)
        .getByText(/Oct 2, 2026/u)
        .closest('time'),
    ).toHaveAttribute('datetime', NEWEST_BACKUP_AT);
  });

  it('says "none yet" and 0 before the first copy', async () => {
    const section = await answeredSection(pendingBackup);

    expect(rows(section)).toEqual([
      ['State', 'Pending. The first backup has not finished yet.'],
      ['Newest backup', 'none yet'],
      ['Backups kept', '0'],
    ]);
  });

  it.each([
    ['database_error', 'The live database could not be read.'],
    ['integrity_failed', 'The copy failed its integrity check and was not kept.'],
    [
      'storage_error',
      'The copy could not be written to storage. The disk may be full, or a permission may be missing.',
    ],
  ] as const)('names a %s in words after a failed attempt', async (kind, words) => {
    inTimeZone('UTC');

    const section = await answeredSection({ ...failedBackup, last_error_kind: kind });

    expect(rows(section)).toEqual([
      ['State', 'Failed. The last scheduled backup did not complete.'],
      ['Newest backup', 'Oct 2, 2026, 3:00 AM'],
      ['Backups kept', '9'],
      ['Last failure', words],
    ]);
  });

  it('shows a failure with no kind as failed, without a "Last failure" row', async () => {
    const section = await answeredSection(failedBackupWithNoKind);

    expect(
      within(section).getByText('Failed. The last scheduled backup did not complete.'),
    ).toBeInTheDocument();
    expect(within(section).queryByText('Last failure')).not.toBeInTheDocument();
    expect(rows(section)).toHaveLength(3);
  });

  it('shows "unknown" for the newest copy and the count when the directory cannot be read', async () => {
    const section = await answeredSection(unreadableBackup);

    expect(rows(section)).toEqual([
      ['State', 'Unreadable. The backup directory cannot be read.'],
      ['Newest backup', 'unknown'],
      ['Backups kept', 'unknown'],
    ]);
    expect(within(section).getAllByText('unknown')).toHaveLength(2);
  });

  it('shows "unknown" for the newest copy whenever the count is unknown', async () => {
    // Only `count` decides: an instant served beside a null count is not shown as known.
    const section = await answeredSection(
      backupStatus({ state: 'unreadable', count: null, latest_at: NEWEST_BACKUP_AT }),
    );

    expect(rows(section)[1]).toEqual(['Newest backup', 'unknown']);
  });

  it.each([
    ['stale', staleBackup, 'Overdue. Scheduled backups have not completed recently.'],
    ['disabled', disabledBackup, 'Disabled. Scheduled backups are switched off on this server.'],
  ])('words %s', async (_, backup, words) => {
    const section = await answeredSection(backup);

    expect(rows(section)[0]).toEqual(['State', words]);
  });

  it('announces its own wait under its heading', async () => {
    server.use(
      http.get(HEALTH_DETAIL_PATH, async () => {
        await delay('infinite');
        return new Response(null);
      }),
    );
    renderWithProviders(<HealthPage />);

    const section = await backupsSection();

    expect(within(section).getByRole('heading', { name: 'Backups', level: 3 })).toBeVisible();
    expect(await within(section).findByRole('status')).toHaveTextContent(
      'Loading backup status...',
    );
  });

  it('reports a failed request as an error, never as a state', async () => {
    server.use(
      http.get(HEALTH_DETAIL_PATH, () =>
        problem(500, 'Internal Server Error', 'The backup status could not be read.'),
      ),
    );
    renderWithProviders(<HealthPage />);

    const section = await backupsSection();
    const alert = await within(section).findByRole('alert');

    expect(within(alert).getByRole('heading', { level: 4 })).toHaveTextContent(
      'Could not load the backup status',
    );
    expect(alert).toHaveTextContent('The backup status could not be read.');
    expect(within(section).queryByText('State')).not.toBeInTheDocument();
    expect(within(section).queryByText(/OK\./u)).not.toBeInTheDocument();
  });

  it('says the backend could not be reached when the request never got an answer', async () => {
    server.use(http.get(HEALTH_DETAIL_PATH, () => HttpResponse.error()));
    renderWithProviders(<HealthPage />);

    const alert = await within(await backupsSection()).findByRole('alert');

    expect(alert).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
  });

  it('is not mounted, and asks nothing, while the health check has not answered', async () => {
    const served = serveBackup(okBackup);
    server.use(
      served.handler,
      http.get(HEALTH_PATH, () => problem(503, 'Service Unavailable', 'Down for a moment.')),
    );
    renderWithProviders(<HealthPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Down for a moment.');
    await settle();

    expect(screen.queryByRole('region', { name: 'Backups' })).not.toBeInTheDocument();
    expect(served.requests()).toBe(0);
  });

  it('comes after the details of the health check', async () => {
    renderPage(okBackup);

    const section = await backupsSection();
    const version = await screen.findByText('Version');

    await waitFor(() => {
      expect(
        version.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });
  });
});
