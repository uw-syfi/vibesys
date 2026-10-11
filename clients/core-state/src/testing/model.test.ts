import {describe, expect, test} from 'bun:test';
import {only, type Step} from '../store.js';
import {checkModel, type StoreModel} from './index.js';

/** A two-slot store: `ask` issues a token into slot `a`, then `b`; a reply clears its slot. */
interface State {
  readonly a: number | null;
  readonly b: number | null;
  readonly next: number;
}
type Action = {readonly type: 'ask'} | {readonly type: 'reply'; readonly token: number};
type Request = {readonly token: number};

const INITIAL: State = {a: null, b: null, next: 1};

function correct(state: State, action: Action): Step<State, Request> {
  if (action.type === 'reply') {
    if (action.token === state.a) return only({...state, a: null});
    if (action.token === state.b) return only({...state, b: null});
    return only(state);
  }
  const token = state.next;
  const slot = state.a === null ? 'a' : 'b';
  return {state: {...state, [slot]: token, next: token + 1}, requests: [{token}]};
}

function model(
  step: StoreModel<State, Action, Request>['step'],
): StoreModel<State, Action, Request> {
  return {
    initial: INITIAL,
    step,
    action: (random, issued) =>
      random.bool() ? {type: 'ask'} : {type: 'reply', token: random.pick(issued)},
    issues: request => request.token,
    awaited: state => ({a: state.a, b: state.b}),
    // A reply answers whichever slot awaits it, so no one slot names its staleness.
    reply: () => null,
  };
}

describe('checkModel', () => {
  test('passes a store whose slots await only tokens it issued', () => {
    expect(() => checkModel(model(correct))).not.toThrow();
  });

  test('fails a store that awaits a token no request issued', () => {
    const unissued = (state: State, action: Action): Step<State, Request> => {
      const next = correct(state, action);
      // Arms the slot but forgets to ask: the reply can never come.
      return next.requests.length > 0 ? only(next.state) : next;
    };
    expect(() => checkModel(model(unissued))).toThrow(/never issued/);
  });

  test('fails a store whose two slots await one token', () => {
    const shared = (state: State, action: Action): Step<State, Request> => {
      if (action.type !== 'ask' || state.a === null) return correct(state, action);
      return only({...state, b: state.a});
    };
    expect(() => checkModel(model(shared))).toThrow(/both await token/);
  });

  test('fails a store that acts on a reply its slot does not await', () => {
    const stale: StoreModel<State, Action, Request> = {
      ...model((state, action) =>
        action.type === 'reply' ? only({...state, a: null}) : correct(state, action),
      ),
      reply: action => (action.type === 'reply' ? {slot: 'a', token: action.token} : null),
    };
    expect(() => checkModel(stale)).toThrow(/reply with token/);
  });
});
