import { QueryClientProvider, useQuery, type QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import {
  adjustmentsQueryKey,
  firstTradesQueryKey,
  useAdjustments,
  useCreateAdjustment,
  useDeleteAdjustment,
  useFirstTrades,
  useReplaceAdjustment,
  type DeleteOutcome,
} from '@/api/adjustments';
import { ApiError } from '@/api/client';
import { createQueryClient } from '@/lib/queryClient';
import {
  ADJUSTMENT_NOT_FOUND_DETAIL,
  bodyOf,
  ETH_PRECISE,
  ethPrecise,
  QUANTITY_NOT_POSITIVE_RULE,
  threeAdjustments,
  threeFirstTrades,
} from '@/test/adjustmentFixtures';
import {
  ADJUSTMENTS_PATH,
  adjustmentPath,
  fakeAdjustments,
  FIRST_TRADES_PATH,
  type FakeAdjustments,
} from '@/test/fakeAdjustments';
import { settle } from '@/test/render';
import { problem, server } from '@/test/server';

/**
 * The hooks of the manual adjustments (spec 027, "Files"), on the shipped query client and
 * against `fakeAdjustments`, which holds every request to the backend's rules.
 *
 * What is pinned here is what the page cannot show on its own: the keys, the exact requests,
 * and what each outcome of a mutation does to the cache - the whole `['accounting']` root on
 * a success and on a delete answered `404` (the adjustment is gone, which is what was asked:
 * spec 027, R13), the list alone on a save answered `404`, nothing on any other failure.
 */

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

/** The fake, registered, with the owner's three adjustments and three first trades. */
function serve(): FakeAdjustments {
  const fake = fakeAdjustments({
    adjustments: threeAdjustments(),
    firstTrades: threeFirstTrades(),
  });
  server.use(...fake.handlers);
  return fake;
}

interface Watch {
  /** How many times the list was read, the first-trades were read, and the probe ran. */
  counts(): { list: number; firstTrades: number; probe: number; outside: number };
}

/**
 * Mounts the two queries of the adjustments, a third under `['accounting']` that no hook
 * knows by name, and a fourth outside the root - and one mutation hook beside them, so its
 * effect on each can be counted.
 */
function mountBeside<T>(
  client: QueryClient,
  fake: FakeAdjustments,
  useMutationHook: () => T,
): { result: { current: T }; watch: Watch } {
  let probe = 0;
  let outside = 0;
  const hook = renderHook(
    () => {
      useAdjustments();
      useFirstTrades();
      useQuery({
        queryKey: ['accounting', 'a-key-no-hook-knows'],
        queryFn: () => {
          probe += 1;
          return 'read';
        },
      });
      useQuery({
        queryKey: ['balances', 'not-accounting'],
        queryFn: () => {
          outside += 1;
          return 'read';
        },
      });
      return useMutationHook();
    },
    { wrapper: wrapperFor(client) },
  );

  return {
    result: hook.result,
    watch: {
      counts: () => ({
        list: fake.count('list'),
        firstTrades: fake.count('first-trades'),
        probe,
        outside,
      }),
    },
  };
}

/** `before`, with everything under `['accounting']` read once more and nothing else. */
function wholeRootReadAgain(before: ReturnType<Watch['counts']>): ReturnType<Watch['counts']> {
  return {
    list: before.list + 1,
    firstTrades: before.firstTrades + 1,
    probe: before.probe + 1,
    outside: before.outside,
  };
}

/** One call of a delete hook's `onGone`: what it was told, and what had been read by then. */
interface Gone {
  readonly outcome: DeleteOutcome;
  readonly counts: ReturnType<Watch['counts']>;
}

/** Waits for the first reads to land, and returns the counts once nothing is moving. */
async function settledCounts(watch: Watch): Promise<ReturnType<Watch['counts']>> {
  await waitFor(() => {
    expect(watch.counts().list).toBeGreaterThan(0);
    expect(watch.counts().firstTrades).toBeGreaterThan(0);
    expect(watch.counts().probe).toBeGreaterThan(0);
  });
  await settle();
  return watch.counts();
}

describe('the query keys', () => {
  it("sit under ['accounting'], the root every adjustment mutation and every sync invalidates", () => {
    expect(adjustmentsQueryKey).toEqual(['accounting', 'adjustments']);
    expect(firstTradesQueryKey).toEqual(['accounting', 'first-trades']);
  });
});

describe('useAdjustments', () => {
  it('reads GET /api/accounting/adjustments and gives the list, as the server sent it', async () => {
    const fake = serve();
    const client = createQueryClient();

    const hook = renderHook(() => useAdjustments(), { wrapper: wrapperFor(client) });
    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });

    expect(fake.requests.map((request) => request.method)).toEqual(['GET']);
    const url = new URL(fake.requests[0]?.url ?? '');
    expect(url.pathname).toBe(ADJUSTMENTS_PATH);
    expect(url.search).toBe('');
    // The array itself, in the endpoint's order, every amount untouched at eighteen places.
    expect(hook.result.current.data).toEqual(threeAdjustments());
    expect(hook.result.current.data?.[2]?.quantity).toBe(ETH_PRECISE.quantity);
    expect(client.getQueryData(adjustmentsQueryKey)).toEqual(threeAdjustments());
  });

  it('fails with the problem document, and is not retried', async () => {
    const fake = serve();
    fake.fail('list', () => problem(500, 'Internal Server Error', 'The list could not be read.'));

    const hook = renderHook(() => useAdjustments(), {
      wrapper: wrapperFor(createQueryClient()),
    });
    await waitFor(() => {
      expect(hook.result.current.isError).toBe(true);
    });
    await settle();

    expect(hook.result.current.error).toBeInstanceOf(ApiError);
    expect((hook.result.current.error as ApiError).problem.detail).toBe(
      'The list could not be read.',
    );
    expect(fake.count('list')).toBe(1);
  });
});

describe('useFirstTrades', () => {
  it('reads GET /api/accounting/first-trades, as the server sent it', async () => {
    const fake = serve();
    const client = createQueryClient();

    const hook = renderHook(() => useFirstTrades(), { wrapper: wrapperFor(client) });
    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });

    expect(fake.requests.map((request) => request.method)).toEqual(['GET']);
    const url = new URL(fake.requests[0]?.url ?? '');
    expect(url.pathname).toBe(FIRST_TRADES_PATH);
    expect(url.search).toBe('');
    expect(hook.result.current.data).toEqual(threeFirstTrades());
    expect(client.getQueryData(firstTradesQueryKey)).toEqual(threeFirstTrades());
  });
});

describe('useCreateAdjustment', () => {
  const BODY = {
    asset: 'SOL',
    quantity: '2.5',
    unit_cost: null,
    occurred_at: '2025-02-27T08:30:00.000Z',
    note: 'Bought in person.',
  };

  it('sends the body it is given as a POST, and gives back what was stored', async () => {
    const fake = serve();
    const { result } = mountBeside(createQueryClient(), fake, () => useCreateAdjustment());

    let created: unknown;
    await act(async () => {
      created = await result.current.mutateAsync(BODY);
    });

    const [request] = fake.requestsTo('create');
    expect(fake.count('create')).toBe(1);
    expect(request?.method).toBe('POST');
    expect(new URL(request?.url ?? '').pathname).toBe(ADJUSTMENTS_PATH);
    expect(request?.contentType).toBe('application/json');
    expect(request?.body).toStrictEqual(BODY);
    expect(created).toMatchObject({
      id: 4,
      asset: 'SOL',
      quantity: '2.500000000000000000',
      unit_cost: null,
      occurred_at: '2025-02-27T08:30:00Z',
    });
  });

  it("invalidates the whole ['accounting'] root, and nothing outside it", async () => {
    const fake = serve();
    const { result, watch } = mountBeside(createQueryClient(), fake, () => useCreateAdjustment());
    const before = await settledCounts(watch);

    await act(async () => {
      await result.current.mutateAsync(BODY);
    });

    // Resolved means read again: the mutation waits for the queries it invalidated.
    const after = watch.counts();
    expect(after.list).toBe(before.list + 1);
    expect(after.firstTrades).toBe(before.firstTrades + 1);
    expect(after.probe).toBe(before.probe + 1);
    expect(after.outside).toBe(before.outside);
  });

  it('invalidates nothing when the server refuses', async () => {
    const fake = serve();
    const { result, watch } = mountBeside(createQueryClient(), fake, () => useCreateAdjustment());
    const before = await settledCounts(watch);

    let failure: unknown;
    await act(async () => {
      failure = await result.current
        .mutateAsync({ ...BODY, quantity: '0' })
        .catch((error: unknown) => error);
    });
    await settle();

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(422);
    expect((failure as ApiError).problem.errors).toEqual([
      { loc: ['body', 'quantity'], msg: QUANTITY_NOT_POSITIVE_RULE },
    ]);
    expect(watch.counts()).toEqual(before);
  });
});

describe('useReplaceAdjustment', () => {
  const BODY = { ...bodyOf(ethPrecise()), note: 'Corrected.' };

  it('sends the five fields as a PUT to the adjustment, and invalidates the root', async () => {
    const fake = serve();
    const { result, watch } = mountBeside(createQueryClient(), fake, () => useReplaceAdjustment());
    const before = await settledCounts(watch);

    await act(async () => {
      await result.current.mutateAsync({ id: 3, body: BODY });
    });

    const [request] = fake.requestsTo('replace');
    expect(fake.count('replace')).toBe(1);
    expect(request?.method).toBe('PUT');
    expect(new URL(request?.url ?? '').pathname).toBe(adjustmentPath(3));
    expect(request?.contentType).toBe('application/json');
    expect(request?.body).toStrictEqual(BODY);
    const after = watch.counts();
    expect(after.list).toBe(before.list + 1);
    expect(after.firstTrades).toBe(before.firstTrades + 1);
    expect(after.probe).toBe(before.probe + 1);
    expect(after.outside).toBe(before.outside);
  });

  it('on a 404, reads the list again and nothing else', async () => {
    const fake = serve();
    const { result, watch } = mountBeside(createQueryClient(), fake, () => useReplaceAdjustment());
    const before = await settledCounts(watch);
    fake.deleteElsewhere(3);

    let failure: unknown;
    await act(async () => {
      failure = await result.current
        .mutateAsync({ id: 3, body: BODY })
        .catch((error: unknown) => error);
    });
    await waitFor(() => {
      expect(watch.counts().list).toBe(before.list + 1);
    });
    await settle();

    expect((failure as ApiError).status).toBe(404);
    expect((failure as ApiError).problem.detail).toBe(ADJUSTMENT_NOT_FOUND_DETAIL);
    expect(watch.counts()).toEqual({ ...before, list: before.list + 1 });
  });

  it.each([
    ['a 422', () => undefined, { ...BODY, quantity: '0' }],
    [
      'a 500',
      (fake: FakeAdjustments) => {
        fake.fail('replace', () => problem(500, 'Internal Server Error', 'Not saved.'));
      },
      BODY,
    ],
    [
      // Not an `ApiError` at all: `fetch` itself rejected.
      'a network failure',
      (fake: FakeAdjustments) => {
        fake.fail('replace', () => HttpResponse.error());
      },
      BODY,
    ],
  ])('on %s, reads nothing again', async (_label, arrange, body) => {
    const fake = serve();
    const { result, watch } = mountBeside(createQueryClient(), fake, () => useReplaceAdjustment());
    const before = await settledCounts(watch);
    arrange(fake);

    await act(async () => {
      await result.current.mutateAsync({ id: 3, body }).catch(() => undefined);
    });
    await settle();

    expect(fake.count('replace')).toBe(1);
    expect(watch.counts()).toEqual(before);
  });
});

describe('useDeleteAdjustment', () => {
  it('sends a DELETE that declares JSON and carries no body', async () => {
    const fake = serve();
    const { result } = mountBeside(createQueryClient(), fake, () =>
      useDeleteAdjustment(() => undefined),
    );

    await act(async () => {
      await result.current.mutateAsync(2);
    });

    const [request] = fake.requestsTo('delete');
    expect(fake.count('delete')).toBe(1);
    expect(request?.method).toBe('DELETE');
    expect(new URL(request?.url ?? '').pathname).toBe(adjustmentPath(2));
    expect(request?.contentType).toBe('application/json');
    expect(request?.text).toBe('');
    expect(fake.adjustments().map((entry) => entry.id)).toEqual([1, 3]);
  });

  it("calls onGone once with 'deleted', after the invalidated queries have been read again", async () => {
    const fake = serve();
    const gone: Gone[] = [];
    const mounted = mountBeside(createQueryClient(), fake, () =>
      useDeleteAdjustment((outcome) => {
        gone.push({ outcome, counts: mounted.watch.counts() });
      }),
    );
    const before = await settledCounts(mounted.watch);

    await act(async () => {
      await mounted.result.current.mutateAsync(2);
    });

    // What `onGone` saw when it ran: the root already asked for again, so whatever it does
    // next - move focus, say "deleted" - it does over the list as it now is.
    expect(gone).toEqual([{ outcome: 'deleted', counts: wholeRootReadAgain(before) }]);
    expect(mounted.result.current.isSuccess).toBe(true);
  });

  it("on a 404, reads the whole root again and then calls onGone once with 'already_deleted'", async () => {
    // The adjustment is gone, which is what was asked, so a 404 takes the road a success
    // takes. The delete made elsewhere moved the positions too: it is the root that is stale,
    // not the list alone.
    const fake = serve();
    const gone: Gone[] = [];
    const mounted = mountBeside(createQueryClient(), fake, () =>
      useDeleteAdjustment((outcome) => {
        gone.push({ outcome, counts: mounted.watch.counts() });
      }),
    );
    const before = await settledCounts(mounted.watch);
    fake.deleteElsewhere(2);

    let failure: unknown;
    await act(async () => {
      failure = await mounted.result.current.mutateAsync(2).catch((error: unknown) => error);
    });
    await settle();

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(404);
    expect((failure as ApiError).problem.detail).toBe(ADJUSTMENT_NOT_FOUND_DETAIL);
    expect(fake.count('delete')).toBe(1);
    expect(gone).toEqual([{ outcome: 'already_deleted', counts: wholeRootReadAgain(before) }]);
    // Once each, and the query outside the root not at all.
    expect(mounted.watch.counts()).toEqual(wholeRootReadAgain(before));
  });

  it.each([
    ['a 204', 'deleted', false],
    ['a 404', 'already_deleted', true],
  ] as const)(
    'after %s, stays pending and tells nobody until the list has been read again',
    async (_label, outcome, goneElsewhere) => {
      // The counts above are of requests that arrived. This is the other half: the answer to
      // the read has to be in before `onGone` runs, or the row that asked would still be on
      // screen when the page says it is gone - and, on a 404, would show an alert about it.
      const fake = serve();
      const gone: DeleteOutcome[] = [];
      const { result, watch } = mountBeside(createQueryClient(), fake, () =>
        useDeleteAdjustment((told) => {
          gone.push(told);
        }),
      );
      const before = await settledCounts(watch);
      if (goneElsewhere) {
        fake.deleteElsewhere(2);
      }
      const release = fake.hold('list');

      act(() => {
        result.current.mutate(2);
      });
      await waitFor(() => {
        expect(watch.counts().list).toBe(before.list + 1);
      });
      await settle();

      // The DELETE has been answered and the list is being read: nothing is over yet.
      expect(fake.count('delete')).toBe(1);
      expect(result.current.isPending).toBe(true);
      expect(result.current.isError).toBe(false);
      expect(gone).toEqual([]);

      release();
      await waitFor(() => {
        expect(gone).toEqual([outcome]);
      });
      await waitFor(() => {
        expect(result.current.isPending).toBe(false);
      });
      // A 404 is still the mutation's error once `onGone` has run. Nothing on the page reads
      // it by then unless the row could not be taken away (the read after it failed).
      expect(result.current.isError).toBe(goneElsewhere);
      expect(gone).toEqual([outcome]);
    },
  );

  it.each([
    ['a 500', () => problem(500, 'Internal Server Error', 'Not deleted.')],
    ['a 403', () => problem(403, 'Forbidden', 'Not allowed.')],
    ['a network failure', () => HttpResponse.error()],
  ])('on %s, tells nobody and reads nothing again', async (_label, respond) => {
    const fake = serve();
    const gone: DeleteOutcome[] = [];
    const { result, watch } = mountBeside(createQueryClient(), fake, () =>
      useDeleteAdjustment((outcome) => {
        gone.push(outcome);
      }),
    );
    const before = await settledCounts(watch);
    fake.fail('delete', respond);

    await act(async () => {
      await result.current.mutateAsync(2).catch(() => undefined);
    });
    await settle();

    expect(gone).toEqual([]);
    expect(fake.count('delete')).toBe(1);
    expect(watch.counts()).toEqual(before);
    expect(fake.adjustments()).toHaveLength(3);
    expect(result.current.isError).toBe(true);
  });
});
