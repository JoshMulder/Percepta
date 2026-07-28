import { useEffect, useState } from "react";

/**
 * Subscribe to a media query.
 *
 * Note that `rem` inside a media query resolves against the browser's *initial*
 * root font size (16px), not the fluid one this console sets. That is what makes
 * it usable as a breakpoint here at all - a breakpoint measured against a root
 * size that itself depends on the viewport would chase its own tail.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
  );

  useEffect(() => {
    const list = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(list.matches);
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
