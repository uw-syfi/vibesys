/**
 * The display zone as the screens receive it. The shell reads the system's zone once, at its edge,
 * and mounts a `DisplayClockProvider` over the window; a screen asks `useDisplayClock()` and hands
 * the clock to whatever formats a time. No screen reads the machine's zone itself.
 */
import {createContext, type JSX, type ReactNode, useContext, useMemo} from 'react';
import {type DisplayClock, makeDisplayClock} from './display-time.js';

/**
 * What a screen shown with no provider above it reads: UTC, a zone that is stated and the same on
 * every machine. The app's window always mounts a provider with the system zone.
 */
const DisplayClockContext = createContext<DisplayClock>(makeDisplayClock('UTC'));

/** Gives the screens below it the clocks of `zone`. */
export function DisplayClockProvider(props: {
  readonly zone: string;
  readonly children: ReactNode;
}): JSX.Element {
  const clock = useMemo(() => makeDisplayClock(props.zone), [props.zone]);
  return (
    <DisplayClockContext.Provider value={clock}>{props.children}</DisplayClockContext.Provider>
  );
}

/** The clocks of the window's display zone. */
export function useDisplayClock(): DisplayClock {
  return useContext(DisplayClockContext);
}
