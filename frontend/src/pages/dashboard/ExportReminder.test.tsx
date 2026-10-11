import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type { ExportReminder as Reminder } from '@/api/exports';
import { ExportReminder } from '@/pages/dashboard/ExportReminder';
import { EXPORT_REMINDER_PATH, MARK_DONE_PATH, nothingOwed, owed } from '@/test/exportFixtures';
import { renderWithProviders, settle } from '@/test/render';
import { problem, refuseNonJsonWrite, server } from '@/test/server';

/** Serves `initial`, and answers each mark by dropping that month, as the backend does. */
function serveReminder(initial: Reminder) {
  let current = initial;
  const marked: string[] = [];
  server.use(
    http.get(EXPORT_REMINDER_PATH, () => HttpResponse.json(current)),
    http.post(MARK_DONE_PATH, ({ request, params }) => {
      const refused = refuseNonJsonWrite(request);
      if (refused !== undefined) {
        return refused;
      }
      const month = String(params.month);
      marked.push(month);
      current = { ...current, months: current.months.filter((each) => each !== month) };
      return HttpResponse.json(current);
    }),
  );
  return { marked };
}

function region() {
  return screen.findByRole('region', { name: 'Monthly exports pending' });
}

describe('ExportReminder', () => {
  it('renders nothing when no month is owed', async () => {
    serveReminder(nothingOwed);
    renderWithProviders(<ExportReminder />);
    await settle();

    expect(screen.queryByRole('region')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('names the month and every exchange, and goes away once marked done', async () => {
    const { marked } = serveReminder(owed('2026-09'));
    const user = userEvent.setup();
    renderWithProviders(<ExportReminder />);

    const reminder = await region();
    expect(reminder).toHaveTextContent(
      'Download the September 2026 transactions from Binance, Bitget, BingX, and Nexo, and save them to your Drive.',
    );

    await user.click(within(reminder).getByRole('button', { name: 'Mark September 2026 as done' }));

    await waitFor(() => {
      expect(screen.queryByRole('region')).toBeNull();
    });
    expect(marked).toEqual(['2026-09']);
  });

  it('keeps every owed month on its own line until each is marked', async () => {
    serveReminder(owed('2026-09', '2026-10'));
    const user = userEvent.setup();
    renderWithProviders(<ExportReminder />);

    const reminder = await region();
    expect(within(reminder).getAllByRole('listitem')).toHaveLength(2);

    await user.click(within(reminder).getByRole('button', { name: 'Mark September 2026 as done' }));

    await waitFor(() => {
      expect(within(reminder).getAllByRole('listitem')).toHaveLength(1);
    });
    expect(within(reminder).getByRole('listitem')).toHaveTextContent('October 2026');
  });

  it('says so when the reminder cannot be checked, rather than showing nothing', async () => {
    server.use(
      http.get(EXPORT_REMINDER_PATH, () =>
        problem(500, 'Internal Server Error', 'The database could not be read.'),
      ),
    );
    renderWithProviders(<ExportReminder />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not check whether the monthly exchange exports are pending: The database could not be read.',
    );
  });

  it('keeps the reminder and says why when marking fails', async () => {
    server.use(
      http.get(EXPORT_REMINDER_PATH, () => HttpResponse.json(owed('2026-09'))),
      http.post(MARK_DONE_PATH, () => problem(409, 'Conflict', '2026-09 has not ended yet')),
    );
    const user = userEvent.setup();
    renderWithProviders(<ExportReminder />);

    const reminder = await region();
    await user.click(within(reminder).getByRole('button', { name: 'Mark September 2026 as done' }));

    expect(await within(reminder).findByRole('alert')).toHaveTextContent(
      'Could not mark September 2026 as done: 2026-09 has not ended yet',
    );
    expect(within(reminder).getAllByRole('listitem')).toHaveLength(1);
  });
});
