import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TOUCH_QUERY, useTouchScreen } from '@/lib/pointer';

/** A `matchMedia` whose one query matches when `matches` says so, and that can say it changed. */
function fakeMatchMedia(initial: boolean) {
  const state = { matches: initial };
  const listeners = new Set<() => void>();
  const matchMedia = vi.fn((media: string) => ({
    media,
    get matches() {
      return media === TOUCH_QUERY && state.matches;
    },
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  }));
  vi.stubGlobal('matchMedia', matchMedia);
  return {
    set(matches: boolean) {
      state.matches = matches;
      listeners.forEach((listener) => {
        listener();
      });
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useTouchScreen', () => {
  it('is false where the browser cannot answer, as in a test', () => {
    vi.stubGlobal('matchMedia', undefined);

    expect(renderHook(() => useTouchScreen()).result.current).toBe(false);
  });

  it('is true on a screen driven by a finger, and false on one driven by a mouse', () => {
    fakeMatchMedia(true);
    expect(renderHook(() => useTouchScreen()).result.current).toBe(true);

    vi.unstubAllGlobals();
    fakeMatchMedia(false);
    expect(renderHook(() => useTouchScreen()).result.current).toBe(false);
  });

  it('follows the screen when its pointer changes', () => {
    const media = fakeMatchMedia(false);
    const { result } = renderHook(() => useTouchScreen());

    act(() => {
      media.set(true);
    });

    expect(result.current).toBe(true);
  });
});
