import {describe, expect, it} from '@effect/vitest';
import {
  APPEARANCE_MODES,
  type Appearance,
  effectiveTheme,
  type ThemeMode,
  themesOf,
} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import type * as Scope from 'effect/Scope';
import fc from 'fast-check';
import {
  type FakePreferencesOptions,
  makeFakePreferencesBridge,
  makeFakeSystemAppearance,
  makeFakeThemeDocument,
} from '../../testing/settings/fakes.js';
import {quiescent, settle} from '../../testing/settle.js';
import {makeAppearance} from './appearance.js';
import {preferencesOver} from './preferences.js';
import {appearanceFromBytes} from './preferences-codec.js';

/** The appearance store over fakes, read through a registry like a screen. */
const open = (system: ThemeMode, preferences: FakePreferencesOptions = {}) =>
  Effect.gen(function* () {
    const bridge = makeFakePreferencesBridge(preferences);
    const fakeSystem = yield* makeFakeSystemAppearance(system);
    const page = makeFakeThemeDocument();
    const store = yield* makeAppearance().pipe(
      Effect.provide(Layer.mergeAll(preferencesOver(bridge.bridge), fakeSystem.layer, page.layer)),
    );
    const registry = AtomRegistry.make();
    const read = Effect.gen(function* () {
      yield* settle;
      return registry.get(store.state);
    });
    const saved = () => {
      const bytes = bridge.stored();
      return bytes === null ? null : appearanceFromBytes(bytes);
    };
    return {store, read, bridge, system: fakeSystem, page, saved};
  });

/** A test whose fibers all run on the counting scheduler `settle` watches. */
const itQuiet = (name: string, body: () => Effect.Effect<void, unknown, Scope.Scope>) =>
  it.effect(name, () => quiescent(body()));

const dark: Appearance = {mode: 'dark', light: 'sand', dark: 'midnight'};
const appearance: fc.Arbitrary<Appearance> = fc.record({
  mode: fc.constantFrom(...APPEARANCE_MODES),
  light: fc.constantFrom(...themesOf('light')),
  dark: fc.constantFrom(...themesOf('dark')),
});

type Step = {readonly choose: Appearance} | {readonly system: ThemeMode};

/** Plays `walk` against a fresh store, then checks what the page shows and what was saved. */
const checkWalk = (walk: readonly Step[]) =>
  Effect.gen(function* () {
    const world = yield* open('light');
    let chosen: Appearance | undefined;
    let system: ThemeMode = 'light';
    for (const step of walk) {
      if ('choose' in step) {
        chosen = step.choose;
        yield* world.store.choose(step.choose);
      } else {
        system = step.system;
        yield* world.system.set(step.system);
      }
      yield* world.read;
    }
    const now = yield* world.read;
    expect(world.page.shown().at(-1)).toBe(effectiveTheme(now.appearance, system));
    if (chosen !== undefined) {
      expect(now.appearance).toEqual(chosen);
      expect(world.saved()).toEqual(chosen);
    }
  });

describe('the appearance of the window', () => {
  itQuiet('shows the saved theme before anything draws', () =>
    Effect.gen(function* () {
      const {page} = yield* open('light', {saved: dark});
      expect(page.shown()).toEqual(['midnight']);
    }),
  );

  itQuiet('with nothing saved follows the system with the default theme of its mode', () =>
    Effect.gen(function* () {
      const light = yield* open('light');
      expect(light.page.shown()).toEqual(['paper']);
      const night = yield* open('dark');
      expect(night.page.shown()).toEqual(['ink']);
    }),
  );

  itQuiet('applies a change of the system live while the mode is system', () =>
    Effect.gen(function* () {
      const {system, page, read} = yield* open('light');
      yield* system.set('dark');
      yield* read;
      expect(page.shown().at(-1)).toBe('ink');
      yield* system.set('light');
      yield* read;
      expect(page.shown().at(-1)).toBe('paper');
    }),
  );

  itQuiet('ignores the system once the person chose a mode', () =>
    Effect.gen(function* () {
      const {store, system, page, read} = yield* open('light', {saved: dark});
      yield* system.set('light');
      yield* store.choose({...dark, mode: 'light'});
      yield* read;
      expect(page.shown().at(-1)).toBe('sand');
      yield* system.set('dark');
      yield* read;
      expect(page.shown().at(-1)).toBe('sand');
    }),
  );

  itQuiet('saves each choice and a restarted app shows it again', () =>
    Effect.gen(function* () {
      const first = yield* open('light');
      yield* first.store.choose(dark);
      expect(first.saved()).toEqual(dark);
      expect((yield* first.read).unsaved).toBe(false);
      const kept = first.saved();
      const second = yield* open('light', kept === null ? {} : {saved: kept});
      expect(second.page.shown()).toEqual(['midnight']);
    }),
  );

  itQuiet(
    'keeps a choice whose save failed, says it is unsaved, and clears that with the next save',
    () =>
      Effect.gen(function* () {
        const {store, page, read, saved} = yield* open('light', {
          failWrite: nth => nth === 0,
        });
        yield* store.choose(dark);
        yield* read;
        expect(page.shown().at(-1)).toBe('midnight');
        expect((yield* read).unsaved).toBe(true);
        expect(saved()).toBeNull();
        yield* store.choose({...dark, mode: 'system'});
        expect((yield* read).unsaved).toBe(false);
        expect(saved()?.mode).toBe('system');
      }),
  );

  itQuiet('starts on the defaults when what was saved cannot be read', () =>
    Effect.gen(function* () {
      const {page} = yield* open('light', {failRead: 'denied'});
      expect(page.shown()).toEqual(['paper']);
    }),
  );

  it.effect(
    'after any choices and system changes shows the theme they come to and has saved the last choice',
    () =>
      Effect.gen(function* () {
        const steps = fc.sample(
          fc.array(
            fc.oneof(
              appearance.map(chosen => ({choose: chosen}) as const),
              fc
                .constantFrom('light' as const, 'dark' as const)
                .map(mode => ({system: mode}) as const),
            ),
            {minLength: 1, maxLength: 12},
          ),
          {numRuns: 25, seed: 7},
        );
        for (const walk of steps) {
          yield* quiescent(
            Effect.scoped(
              Effect.gen(function* () {
                yield* checkWalk(walk);
              }),
            ),
          );
        }
      }),
  );
});
