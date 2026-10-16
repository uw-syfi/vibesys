/**
 * The folder field's path completion: the hosts' completion reducer (`completionStep`, the one
 * Machines folds), its listings asked over the welcome bridge after the reducer's pause on the
 * injected schedule. A newer listing for the field replaces one still waiting, and a late reply
 * is dropped by the reducer's token.
 */
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionField,
  type CompletionState,
  completionStep,
} from '@vibesys/hosts';
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/machine';
import {createStore, type Store} from '@vibesys/store';

/** The bridge call path completion makes. */
export type CompletionBridge = Pick<WelcomeBridge, 'complete'>;

/** The folder field's completion store over `bridge`, its pauses on `schedule`. */
export function createFolderCompletion(
  bridge: CompletionBridge,
  schedule: ScheduleTimeout,
): Store<CompletionState, CompletionAction> {
  const waiting = new Map<CompletionField, () => void>();
  return createStore(completionStep, COMPLETION_EMPTY, (request, dispatch) => {
    waiting.get(request.field)?.();
    waiting.set(
      request.field,
      schedule(() => {
        waiting.delete(request.field);
        void settled(bridge.complete(request.host, request.directory)).then(result =>
          dispatch({type: 'reply', field: request.field, token: request.token, result}),
        );
      }, request.afterMs),
    );
  });
}
