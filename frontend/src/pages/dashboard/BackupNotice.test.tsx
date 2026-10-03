import { act, screen, waitFor, within } from '@testing-library/react';
import { delay, http } from 'msw';
import { describe, expect, it } from 'vitest';

import type { BackupStatus } from '@/api/health';
import { healthDetailQueryKey } from '@/api/health';
import { BackupNotice } from '@/pages/dashboard/BackupNotice';
import {
  disabledBackup,
  failedBackup,
  failedBackupWithNoKind,
  failedBackupWithNone,
  HEALTH_DETAIL_PATH,
  okBackup,
  pendingBackup,
  serveBackup,
  staleBackup,
  staleBackupWithNone,
  unreadableBackup,
} from '@/test/backupFixtures';
import { renderWithProviders, settle } from '@/test/render';
import { problem, server } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/** `Intl` separates the time from "AM" with a narrow no-break space; compare words. */
function plain(text: string | null): string {
  return (text ?? '').replace(/\s+/gu, ' ');
}

function renderNotice(backup: BackupStatus) {
  const served = serveBackup(backup);
  server.use(served.handler);
  const rendered = renderWithProviders(<BackupNotice />);

  return { ...rendered, requests: served.requests };
}

/** Wait until the detail was answered and rendered, so "nothing" is not "not yet". */
async function answered(requests: () => number): Promise<void> {
  await waitFor(() => {
    expect(requests()).toBeGreaterThan(0);
  });
  await settle();
}

describe('BackupNotice', () => {
  it.each([
    [
      'failed, with a newest copy',
      failedBackup,
      'The last scheduled backup failed. The newest backup is from Oct 2, 2026, 3:00 AM. Open backend health',
    ],
    [
      'failed, with none',
      failedBackupWithNone,
      'The last scheduled backup failed. There is no backup yet. Open backend health',
    ],
    [
      'failed by a defect, with no kind',
      failedBackupWithNoKind,
      'The last scheduled backup failed. The newest backup is from Oct 2, 2026, 3:00 AM. Open backend health',
    ],
    [
      'stale, with a newest copy',
      staleBackup,
      'The newest backup is from Oct 2, 2026, 3:00 AM. Scheduled backups have not completed since. Open backend health',
    ],
    ['stale, with none', staleBackupWithNone, 'There is no backup yet. Open backend health'],
    [
      'unreadable',
      unreadableBackup,
      'The backup directory cannot be read, so it is not known whether backups are being kept. Open backend health',
    ],
  ])(
    'warns when %s, in one alert ending with a link to the Health page',
    async (_, backup, text) => {
      inTimeZone('UTC');
      renderNotice(backup);

      const alert = await screen.findByRole('alert');

      expect(plain(alert.textContent)).toBe(text);
      expect(alert.tagName).toBe('P');
      expect(within(alert).getByRole('link', { name: 'Open backend health' })).toHaveAttribute(
        'href',
        '/health',
      );
      expect(screen.getAllByRole('alert')).toHaveLength(1);
    },
  );

  it.each([
    ['ok', okBackup],
    ['pending', pendingBackup],
    ['disabled', disabledBackup],
  ])('renders nothing when the state is %s', async (_, backup) => {
    const { container, requests } = renderNotice(backup);

    await answered(requests);

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing while the request is in flight', async () => {
    let asked = 0;
    server.use(
      http.get(HEALTH_DETAIL_PATH, async () => {
        asked += 1;
        await delay('infinite');
        return new Response(null);
      }),
    );

    const { container } = renderWithProviders(<BackupNotice />);
    await waitFor(() => {
      expect(asked).toBeGreaterThan(0);
    });
    await settle();

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the request fails: the Health page reports that', async () => {
    let asked = 0;
    server.use(
      http.get(HEALTH_DETAIL_PATH, () => {
        asked += 1;
        return problem(500, 'Internal Server Error', 'The detail could not be read.');
      }),
    );

    const { container } = renderWithProviders(<BackupNotice />);
    await waitFor(() => {
      expect(asked).toBeGreaterThan(0);
    });
    await settle();

    expect(container).toBeEmptyDOMElement();
  });

  it('keeps the warning when a later poll fails: the last reading is the only one there is', async () => {
    const { queryClient } = renderNotice(failedBackup);
    await screen.findByRole('alert');
    server.use(
      http.get(HEALTH_DETAIL_PATH, () =>
        problem(500, 'Internal Server Error', 'The detail could not be read.'),
      ),
    );

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: healthDetailQueryKey });
    });
    // TanStack notifies its observers on a timer of its own; let the re-render land.
    await settle();

    expect(queryClient.getQueryState(healthDetailQueryKey)?.status).toBe('error');
    expect(screen.getByRole('alert')).toHaveTextContent('The last scheduled backup failed.');
  });

  // The counterpart of the test above, and the proof that `settle()` lets a re-render land:
  // without it, "the warning stayed" would be true of a page that had not re-rendered yet.
  it('goes away when a later poll answers ok', async () => {
    const { queryClient } = renderNotice(staleBackup);
    await screen.findByRole('alert');
    server.use(serveBackup(okBackup).handler);

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: healthDetailQueryKey });
    });
    // TanStack notifies its observers on a timer of its own; let the re-render land.
    await settle();

    expect(queryClient.getQueryData(healthDetailQueryKey)).toEqual({ backup: okBackup });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
