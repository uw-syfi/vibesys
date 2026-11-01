/**
 * Which stores a screen wakes when it is first shown: the one place that says, for every screen,
 * whose reads wait for it. A host read is a fresh process, and a cold launch's first answer waits
 * its turn behind all of them, so a launch starts the reads of the screen it opens on and of the
 * rail, and each other screen starts its own when it is first selected. The stores take one
 * action for it (`Open`, from `@vibesys/store`) and ask nothing before it; this table is the only
 * place that knows which screen needs which, so a screen added to the shell must be added here (the
 * table is typed over every screen kind).
 */
import type {Screen} from '@vibesys/feature-app-shell/state';
import {OPEN, type Openable} from '@vibesys/store';

/** What the main window's rail shows beside the selected screen, and the stores that decide it. */
const RAIL: readonly (keyof Sleepers)[] = ['machines'];

/** The stores whose reads wait for a screen to be shown. */
export interface Sleepers {
  /** The machines' checks: their status and what needs attention, on Machines and in Settings. */
  readonly machines: Openable;
  /** New run's reads: its machine's facts, harnesses and examples, and what it has set up. */
  readonly newRun: Openable;
}

const WAKES: Readonly<Record<Screen['kind'], readonly (keyof Sleepers)[]>> = {
  runs: [],
  usage: [],
  // A run's page reads through its own session.
  run: [],
  machines: ['machines'],
  // Settings counts the machines that need attention beside its sections, and embeds Machines.
  settings: ['machines'],
  'new-run': ['newRun'],
};

/** Wakes the stores `screen` needs: each starts its reads on the first time, and none again. */
export function openScreen(sleepers: Sleepers, screen: Screen): void {
  for (const store of WAKES[screen.kind]) sleepers[store].dispatch(OPEN);
}

/**
 * The rail is on screen from the first frame, and its Machines mark warns of a machine that cannot
 * be reached: that rests on the machines' checks, so they are visible reads. They start once Runs
 * has read what it shows (`settled`), not at launch beside its reads and not on a timer, so the
 * first screen's reads never wait behind them. `sources` are the stores `settled` is computed from.
 */
export function openRailWhenSettled(
  sleepers: Sleepers,
  sources: readonly Subscribable[],
  settled: () => boolean,
): void {
  const stops: (() => void)[] = [];
  const wake = (): void => {
    if (!settled()) return;
    for (const stop of stops.splice(0)) stop();
    for (const store of RAIL) sleepers[store].dispatch(OPEN);
  };
  for (const source of sources) stops.push(source.subscribe(wake));
  wake();
}

/** What `settled` is computed from: a store that says when it changed. */
interface Subscribable {
  subscribe(listener: () => void): () => void;
}
