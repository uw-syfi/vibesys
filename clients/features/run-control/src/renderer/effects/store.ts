/**
 * The run-control feature's shell: its store, each run command performed through the injected
 * `RunControlBridge` and its reply dispatched back with the command's token.
 */

import {createStore, type Store} from '@vibesys/core-state';
import {settled, type WelcomeResult} from '@vibesys/ipc';
import {
  type RunCommand,
  type RunControlBridge,
  WELCOME_INITIAL,
  type WelcomeAction,
  type WelcomeState,
  welcomeStep,
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
  return createStore(welcomeStep, WELCOME_INITIAL, (request, dispatch) => {
    void settled(command(bridge, confirm, request.command)).then(result =>
      dispatch({type: 'reply', token: request.token, result}),
    );
  });
}
