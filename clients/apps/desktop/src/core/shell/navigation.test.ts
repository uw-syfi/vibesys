import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import type * as Scope from 'effect/Scope';
import {initialNavigation, type KeyPress} from '../../state/index.js';
import {quiescent, settle} from '../../testing/settle.js';
import {makeFakeKeyPresses} from '../../testing/shell/fake-key-presses.js';
import {makeNavigation} from './navigation.js';

const plain: KeyPress = {
  key: 'Escape',
  alt: false,
  ctrl: false,
  meta: false,
  shift: false,
  handled: false,
  capturing: false,
};

/** The navigation store over fake keys, read through a registry like a screen. */
const open = Effect.gen(function* () {
  const keys = yield* makeFakeKeyPresses;
  const store = yield* makeNavigation().pipe(Effect.provide(keys.layer));
  const registry = AtomRegistry.make();
  const read = Effect.gen(function* () {
    yield* settle;
    return registry.get(store.state);
  });
  return {store, keys, read};
});

const itQuiet = (name: string, body: () => Effect.Effect<void, unknown, Scope.Scope>) =>
  it.effect(name, () => quiescent(body()));

describe('the navigation of the window', () => {
  itQuiet('starts on the run list', () =>
    Effect.gen(function* () {
      const {read} = yield* open;
      expect(yield* read).toEqual(initialNavigation);
    }),
  );

  itQuiet('Escape and Alt+Left go back from Settings, and not where a control uses them', () =>
    Effect.gen(function* () {
      const {store, keys, read} = yield* open;
      yield* store.go({type: 'open-settings'});
      yield* keys.press({...plain, capturing: true});
      expect((yield* read).destination).toBe('settings');
      yield* keys.press({...plain, key: 'Enter'});
      expect((yield* read).destination).toBe('settings');
      yield* keys.press(plain);
      expect((yield* read).destination).toBe('runs');
      yield* store.go({type: 'open-settings'});
      yield* keys.press({...plain, key: 'ArrowLeft', alt: true});
      expect((yield* read).destination).toBe('runs');
    }),
  );

  itQuiet(
    'back from an agent, then the agents, then the run, ends on the list and stays there',
    () =>
      Effect.gen(function* () {
        const {store, keys, read} = yield* open;
        yield* store.go({type: 'open-run', host: 'host-a', runId: 'run-1'});
        yield* store.go({type: 'pick-agent', agentId: 'a'});
        yield* keys.press(plain);
        expect((yield* read).runs).toMatchObject({screen: 'run', view: 'agents', agentId: null});
        yield* keys.press(plain);
        expect((yield* read).runs).toMatchObject({screen: 'run', view: 'report', agentId: null});
        yield* keys.press(plain);
        expect((yield* read).runs).toEqual({screen: 'runs'});
        yield* keys.press(plain);
        // The list is as it was, except that it remembers the row the run was opened from.
        expect(yield* read).toEqual({
          ...initialNavigation,
          fleet: {...initialNavigation.fleet, selected: 'host-a/run-1'},
        });
      }),
  );

  itQuiet(
    'keeps the list filters through a run and the settings, and Escape does not clear them',
    () =>
      Effect.gen(function* () {
        const {store, keys, read} = yield* open;
        yield* store.go({type: 'filter-runs-by-state', state: 'live'});
        yield* store.go({type: 'filter-runs-by-host', host: 'host-b'});
        yield* store.go({type: 'open-run', host: 'host-b', runId: 'run-2'});
        yield* store.go({type: 'open-settings'});
        yield* keys.press(plain);
        yield* keys.press(plain);
        expect((yield* read).runs).toEqual({screen: 'runs'});
        expect((yield* read).fleet).toEqual({
          ...initialNavigation.fleet,
          state: 'live',
          host: 'host-b',
          selected: 'host-b/run-2',
        });
        yield* keys.press(plain);
        expect((yield* read).fleet).toEqual({
          ...initialNavigation.fleet,
          state: 'live',
          host: 'host-b',
          selected: 'host-b/run-2',
        });
      }),
  );
});
