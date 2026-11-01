/**
 * Opening a run in the shell drawn in a DOM (`openShell` with `run`), as a person does: from the
 * dashboard's button for it. Returns once the Run screen has read the run and shows its header.
 */
import {act} from 'react';
import {maybe, one, until} from './dom-query.js';
import type {OpenShell} from './shell-world.js';

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
