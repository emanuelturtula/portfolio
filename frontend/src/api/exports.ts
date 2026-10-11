/**
 * Fetcher and hooks for the monthly export reminder (spec 040): `GET /api/exports/reminder`
 * and `POST /api/exports/months/{month}/done`.
 *
 * Types come from the generated schema and nowhere else.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type ExportReminder = components['schemas']['ExportReminderResponse'];

const EXPORT_REMINDER_PATH = '/api/exports/reminder';

/**
 * Polled every hour: a month ends once, so a dashboard left open across midnight on the 1st
 * shows the reminder within the hour. The endpoint reads one small table.
 */
const REFETCH_INTERVAL_MS = 60 * 60_000;

export const exportReminderQueryKey = ['exports', 'reminder'] as const;

export function useExportReminder(): UseQueryResult<ExportReminder> {
  return useQuery({
    queryKey: exportReminderQueryKey,
    queryFn: ({ signal }) => apiFetch<ExportReminder>(EXPORT_REMINDER_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/** Marks a month (`YYYY-MM`) done. The answer is the new reminder, written straight to the cache. */
export function useMarkExportMonthDone(): UseMutationResult<ExportReminder, unknown, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (month: string) =>
      apiFetch<ExportReminder>(`/api/exports/months/${encodeURIComponent(month)}/done`, {
        method: 'POST',
      }),
    onSuccess: (reminder) => {
      queryClient.setQueryData(exportReminderQueryKey, reminder);
    },
  });
}
