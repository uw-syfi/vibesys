import {useEffect, useState} from 'react';

/** How often a ticking clock re-renders its component, in milliseconds. */
const NOW_TICK_MS = 1000;

/**
 * The current time in epoch milliseconds from `clock`, re-read every `NOW_TICK_MS` while
 * `ticking`. While not ticking it holds the last value and schedules nothing, so a settled page
 * pays no re-render for a clock it does not show.
 */
export function useNow(clock: () => number, ticking: boolean): number {
  const [now, setNow] = useState(clock);
  useEffect(() => {
    if (!ticking) return undefined;
    setNow(clock());
    const timer = setInterval(() => setNow(clock()), NOW_TICK_MS);
    return () => clearInterval(timer);
  }, [clock, ticking]);
  return now;
}
