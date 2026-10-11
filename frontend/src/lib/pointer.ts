import { useSyncExternalStore } from 'react';

/**
 * A screen whose main pointer is a finger: it cannot hover, and what it points with covers
 * part of what it points at. A mouse or a stylus that hovers does not match.
 */
export const TOUCH_QUERY = '(hover: none) and (pointer: coarse)';

function query(): MediaQueryList | undefined {
  // jsdom, and an old browser, have no `matchMedia`: treat them as a mouse.
  return typeof window.matchMedia === 'function' ? window.matchMedia(TOUCH_QUERY) : undefined;
}

function subscribe(onChange: () => void): () => void {
  const list = query();
  list?.addEventListener('change', onChange);
  return () => {
    list?.removeEventListener('change', onChange);
  };
}

/**
 * Whether the main pointer is a finger, following the screen if that changes (a tablet docked
 * to a keyboard and mouse, say).
 */
export function useTouchScreen(): boolean {
  return useSyncExternalStore(subscribe, () => query()?.matches ?? false);
}
