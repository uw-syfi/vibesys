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
