import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { BackupStatus } from '@/api/health';
import { failedBackup, okBackup, serveBackup, unreadableBackup } from '@/test/backupFixtures';
import { fakeAccounting } from '@/test/fakeAccounting';
import { fakeExchanges } from '@/test/fakeExchanges';
import { fakePortfolio } from '@/test/fakePortfolio';
import { healthyPortfolio } from '@/test/fixtures';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, server, TEST_USERNAME } from '@/test/server';
import { GAINING_SUMMARY } from '@/test/summaryFixtures';

/**
 * The backup warning in its place on the dashboard (spec 029): above the figures, and ending
 * with a link that reaches the Health page's account of what happened.
 */
function openDashboard(backup: BackupStatus): () => number {
  const served = serveBackup(backup);
  server.use(
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...fakePortfolio({ ...healthyPortfolio(), summary: GAINING_SUMMARY }).handlers,
    ...fakeAccounting().handlers,
    ...fakeExchanges().handlers,
    served.handler,
  );
  renderApp(['/']);
  return served.requests;
}

function backupAlert(): HTMLElement | undefined {
  return screen
    .queryAllByRole('alert')
    .find((alert) => alert.textContent.includes('Open backend health'));
}

describe('the dashboard and the backup warning', () => {
  it('puts the warning first, above the figures and the holdings', async () => {
    openDashboard(failedBackup);

    const total = await screen.findByRole('region', { name: 'Total value' });
    const holdings = await screen.findByRole('region', { name: 'Holdings' });
    const alert = await screen.findByText(/The last scheduled backup failed\./u);

    expect(alert.compareDocumentPosition(total) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(alert.compareDocumentPosition(holdings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The first thing in the page, which is the one element in the main landmark.
    expect(screen.getByRole('main').firstElementChild?.firstElementChild).toBe(alert);
  });

  it('warns that the directory cannot be read', async () => {
    openDashboard(unreadableBackup);

    expect(
      await screen.findByText(
        /The backup directory cannot be read, so it is not known whether backups are being kept\./u,
      ),
    ).toHaveAttribute('role', 'alert');
  });

  it('says nothing about backups that are working', async () => {
    const requests = openDashboard(okBackup);

    await screen.findByRole('region', { name: 'Total value' });
    await settle();

    expect(requests()).toBeGreaterThan(0);
    expect(backupAlert()).toBeUndefined();
  });

  it('links to the Health page, which says why', async () => {
    const user = userEvent.setup();
    openDashboard(failedBackup);
    const alert = await screen.findByText(/The last scheduled backup failed\./u);

    await user.click(within(alert).getByRole('link', { name: 'Open backend health' }));

    const section = await screen.findByRole('region', { name: 'Backups' });
    expect(currentPath()).toBe('/health');
    expect(
      await within(section).findByText('Failed. The last scheduled backup did not complete.'),
    ).toBeInTheDocument();
  });
});
