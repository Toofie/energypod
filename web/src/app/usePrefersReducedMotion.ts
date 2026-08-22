import { useCallback, useSyncExternalStore } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function query(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(QUERY) : null;
}

/**
 * The operator's reduced-motion preference, read through matchMedia so the
 * preference is actually consulted (and respected) rather than assumed.
 */
export function usePrefersReducedMotion(): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    const media = query();
    if (media === null) {
      return () => undefined;
    }
    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);
  const read = useCallback(() => query()?.matches ?? false, []);
  return useSyncExternalStore(subscribe, read);
}
