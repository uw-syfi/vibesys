/**
 * The start-run feature's shell: the sheet's store, each request performed through the injected
 * `StartRunBridge` and its reply dispatched back with the request's token.
 */

import {settled} from '@vibesys/ipc';
import {createStore, type Store} from '@vibesys/store';
import {
  START_CLOSED,
  type StartAction,
  type StartRunBridge,
  type StartState,
  startStep,
} from '../../state/index.js';

export function createStartStore(bridge: StartRunBridge): Store<StartState, StartAction> {
  return createStore(startStep, START_CLOSED, (request, dispatch) => {
    switch (request.kind) {
      case 'tasks':
        void settled(bridge.tasks(request.host, request.project)).then(result =>
          dispatch({type: 'tasks-reply', token: request.token, result}),
        );
        return;
      case 'harnesses':
        void settled(bridge.harnesses(request.host)).then(result =>
          dispatch({type: 'harnesses-reply', token: request.token, result}),
        );
        return;
      case 'start':
        void settled(
          bridge.start(
            request.host,
            request.project,
            request.task,
            request.args,
            request.harness,
            request.token,
          ),
        ).then(result => dispatch({type: 'start-reply', token: request.token, result}));
        return;
      case 'cancel-start':
        // The start's own reply ends the cancelling state; this one carries nothing new.
        void settled(bridge.cancelStart(request.token));
        return;
    }
  });
}
