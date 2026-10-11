/**
 * The replay demo's fixture load: loading, loaded, or failed with a Retry. Each load asks for the
 * fixture under a fresh attempt number (so a retry is never served from cache) and carries a
 * token; a reply for any other load is dropped, so a superseded load never folds its events.
 */
import type {RunEvent} from '@vibesys/backend-client';
import {only, type Step} from '@vibesys/core-state';

export const DEFAULT_REPLAY_FIXTURE_URL = '/__vibesys/fixtures/framework-events.jsonl';

export type ReplayState =
  | {readonly kind: 'idle'; readonly next: number}
  | {readonly kind: 'loading'; readonly token: number; readonly next: number}
  | {readonly kind: 'loaded'; readonly next: number}
  | {readonly kind: 'failed'; readonly error: string; readonly next: number};

type ReplayResult =
  | {readonly ok: true; readonly events: readonly RunEvent[]}
  | {readonly ok: false; readonly error: string};

export type ReplayAction =
  /** Load the fixture: on start, and the failure banner's Retry. */
  | {readonly type: 'load'}
  | {readonly type: 'reply'; readonly token: number; readonly result: ReplayResult};

export type ReplayRequest =
  | {readonly kind: 'fetch'; readonly token: number; readonly url: string}
  /** Fold the loaded events into the run. */
  | {readonly kind: 'append'; readonly events: readonly RunEvent[]};

export const REPLAY_IDLE: ReplayState = {kind: 'idle', next: 0};

/** What the failure banner says, or null when there is no failure to show. */
export function replayFailure(state: ReplayState): string | null {
  return state.kind === 'failed' ? state.error : null;
}

export function replayStep(
  state: ReplayState,
  action: ReplayAction,
): Step<ReplayState, ReplayRequest> {
  switch (action.type) {
    case 'load': {
      if (state.kind === 'loading' || state.kind === 'loaded') return only(state);
      const token = state.next;
      return {
        state: {kind: 'loading', token, next: token + 1},
        requests: [{kind: 'fetch', token, url: `${DEFAULT_REPLAY_FIXTURE_URL}?attempt=${token}`}],
      };
    }
    case 'reply': {
      if (state.kind !== 'loading' || state.token !== action.token) return only(state);
      const {result} = action;
      if (!result.ok) return only({kind: 'failed', error: result.error, next: state.next});
      return {
        state: {kind: 'loaded', next: state.next},
        requests: [{kind: 'append', events: result.events}],
      };
    }
  }
}
