/**
 * The run-control feature's shell: the machine's actor, its `command` operation performed through
 * the injected `RunControlBridge`, seen as the store the view subscribes to.
 */

import {settled, type WelcomeResult} from '@vibesys/ipc';
import {actorStore} from '@vibesys/machine';
import type {Store} from '@vibesys/store';
import {createActor, fromPromise} from 'xstate';
import {
  type RunCommand,
  type RunControlBridge,
  runControlMachine,
  type WelcomeAction,
  type WelcomeState,
  welcomeView,
} from '../../state/index.js';

/** Ask the user a yes/no question (the window's native confirm). */
export type Confirm = (question: string) => boolean;

function command(
  bridge: RunControlBridge,
  confirm: Confirm,
  run: RunCommand,
): Promise<WelcomeResult<null>> {
  switch (run.kind) {
    case 'attach':
      return bridge.attach(run.host, run.instance);
    case 'resume': {
      // A resume continues the run's agents, which costs tokens: ask first.
      if (!confirm(`Resume run ${run.run} in ${run.project}? It continues the run's agents.`)) {
        return Promise.resolve({ok: true, value: null});
      }
      return bridge.resume(run.host, run.project, run.run);
    }
    case 'stop':
      return bridge.stop(run.host, run.instance, run.force);
    case 'retry':
      return bridge.retry();
    case 'show-run':
      return bridge.showRun();
    case 'show-welcome':
      return bridge.showWelcome();
  }
}

/** The run-control store over `bridge`; `confirm` asks before a resume. */
export function createRunControlStore(
  bridge: RunControlBridge,
  confirm: Confirm,
): Store<WelcomeState, WelcomeAction> {
  const machine = runControlMachine.provide({
    actors: {
      command: fromPromise<WelcomeResult<null>, RunCommand>(({input}) =>
        settled(command(bridge, confirm, input)),
      ),
    },
  });
  // The actor lives as long as the window that renders it, so it is intentionally never stopped.
  return actorStore(createActor(machine).start(), welcomeView);
}
