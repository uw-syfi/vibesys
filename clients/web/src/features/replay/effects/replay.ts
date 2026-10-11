/**
 * The replay demo's shell: it runs the fixture-load core (`state/replay.ts`), fetches the fixture
 * through the injected `load`, and hands the loaded events to `onEvents` (the composition folds
 * them into the run view).
 */
import type {RunEvent} from '@vibesys/contracts';
import {createStore, type Store} from '@vibesys/core-state';
import {REPLAY_IDLE, type ReplayAction, type ReplayState, replayStep} from '../state/replay.js';

/** Load a replay fixture's validated events; rejects with the reason the page shows. */
type LoadFixture = (url: string) => Promise<readonly RunEvent[]>;

export function createReplayStore(
  onEvents: (events: readonly RunEvent[]) => void,
  load: LoadFixture,
): Store<ReplayState, ReplayAction> {
  return createStore(replayStep, REPLAY_IDLE, (request, dispatch) => {
    if (request.kind === 'append') {
      onEvents(request.events);
      return;
    }
    const {token} = request;
    load(request.url).then(
      events => dispatch({type: 'reply', token, result: {ok: true, events}}),
      (reason: unknown) =>
        dispatch({
          type: 'reply',
          token,
          result: {ok: false, error: reason instanceof Error ? reason.message : String(reason)},
        }),
    );
  });
}
