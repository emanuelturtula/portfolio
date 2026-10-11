import type { ExportReminder } from '@/api/exports';

export const EXPORT_REMINDER_PATH = '/api/exports/reminder';
export const MARK_DONE_PATH = '/api/exports/months/:month/done';

export const EXCHANGES = ['Binance', 'Bitget', 'BingX', 'Nexo'];

/** Nothing owed: the default answer, which renders no reminder. */
export const nothingOwed: ExportReminder = { months: [], exchanges: EXCHANGES };

export function owed(...months: string[]): ExportReminder {
  return { months, exchanges: EXCHANGES };
}
