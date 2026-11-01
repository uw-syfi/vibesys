/**
 * Opening a run in the shell drawn in a DOM (`openShell` with `run`), as a person does: from the
 * dashboard's button for it. Returns once the Run screen has read the run and shows its header.
 */

import type {RunLiveness} from '@vibesys/proto';
import type {StoreRunFixture} from '@vibesys/run-fixtures';
import {fixtureNamed, type RunFixture} from '@vibesys/run-fixtures';
import type {ScheduleTimeout} from '@vibesys/transport';
import {IDBFactory} from 'fake-indexeddb';
import {act} from 'react';
import {maybe, one, until} from './dom-query.js';
import {seedOf} from './fixture-seed.js';
import {wire} from './gateway-fixture.js';
import type {ReplayWorld} from './replay-world.js';
import {type OpenShell, openShell} from './shell-world.js';

/** The run the journeys' worlds list on the dashboard: the queue example's task. */
const RUN_BUTTON = /^queue-rs · mpmc/;

export async function openRun(shell: OpenShell, button: RegExp = RUN_BUTTON): Promise<void> {
  const main = one(shell.container, {role: 'main'});
  const press = one(main, {role: 'button', name: button});
  await act(async () => press.click());
  await until(
    'the Run screen reading its run',
    () =>
      maybe(shell.container, {role: 'heading', name: 'queue-rs · mpmc'}) !== null &&
      shell.container.querySelector('main[data-state="ready"]') !== null,
  );
}

/** The Fake gateway serving the records of `fixture` as one batch, the host saying `liveness` of the run (default: from its end). */
export function storedWorld(fixture: StoreRunFixture, liveness?: RunLiveness): ReplayWorld {
  return {input: wire(fixture, liveness), updates: {}};
}

/**
 * The shell on `world`, with the run open: the dashboard lists `listed` (the finished run of the
 * journeys' fixture world, whose listing the run's records do not change).
 */
export async function shellOnRun(
  world: ReplayWorld,
  schedule: ScheduleTimeout,
  listed: RunFixture = {...fixtureNamed('finished-run'), runId: 'run-a'},
): Promise<OpenShell> {
  // A fresh copy keeps nothing the last one cached.
  Object.assign(globalThis, {indexedDB: new IDBFactory()});
  const shell = await openShell(null, {schedule, seed: seedOf(listed, 'ink'), run: world});
  await openRun(shell);
  return shell;
}
