/**
 * Model-based checking of a screen store's reducer: seeded random sequences of user actions and
 * replies (stale, duplicated, and reordered ones included, since a reply's token is drawn from every
 * token issued so far) are folded through the reducer, and after every step:
 *
 * - the store's `invariants` hold;
 * - a reply whose token is not the one its slot awaits leaves the state as it was and asks nothing.
 *
 * A failing seed is reported in the assertion message, to pin as an example.
 */
import {expect} from 'bun:test';
import {SEEDS, Seeded} from './seeded.js';

export interface StoreModel<S, A, R> {
  readonly initial: S;
  readonly step: (state: S, action: A) => {readonly state: S; readonly requests: readonly R[]};
  /** One random action; replies take their token from `issued`. */
  readonly action: (random: Seeded, issued: readonly number[], state: S) => A;
  /** The token a request issues, or null for one that awaits no reply. */
  readonly issues: (request: R) => number | null;
  /** For a reply action, its token and the token `state` awaits in its slot; null otherwise. */
  readonly reply: (
    state: S,
    action: A,
  ) => {readonly token: number; readonly awaited: number | null} | null;
  readonly invariants: (state: S) => string[];
  /** More properties of one step. */
  readonly also?: (before: S, action: A, after: S, requests: readonly R[]) => void;
}

export function checkModel<S, A, R>(model: StoreModel<S, A, R>, steps = 40): void {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    let state = model.initial;
    const issued: number[] = [0];
    for (let step = 0; step < steps; step += 1) {
      const action = model.action(random, issued, state);
      const before = state;
      const next = model.step(state, action);
      state = next.state;
      for (const request of next.requests) {
        const token = model.issues(request);
        if (token !== null) issued.push(token);
      }
      expect({seed, step, problems: model.invariants(state)}).toEqual({seed, step, problems: []});
      const reply = model.reply(before, action);
      if (reply !== null && reply.awaited !== reply.token) {
        expect({seed, step, stale: next.state === before && next.requests.length === 0}).toEqual({
          seed,
          step,
          stale: true,
        });
      }
      model.also?.(before, action, state, next.requests);
    }
  }
}
