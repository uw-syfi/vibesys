import {describe, expect, it} from '@effect/vitest';
import type {Appearance} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import {makeFakePreferencesBridge} from '../../testing/settings/fakes.js';
import {Preferences, PreferencesError, preferencesOver} from './preferences.js';
import {appearanceFromBytes} from './preferences-codec.js';

const dark: Appearance = {mode: 'dark', light: 'sand', dark: 'terminal'};

const over = (bridge: Parameters<typeof preferencesOver>[0]) =>
  Effect.gen(function* () {
    return yield* Preferences;
  }).pipe(Effect.provide(preferencesOver(bridge)));

describe('the saved preferences', () => {
  it.effect('read as nothing before anything is saved', () =>
    Effect.gen(function* () {
      const preferences = yield* over(makeFakePreferencesBridge().bridge);
      expect(yield* preferences.load).toBeNull();
    }),
  );

  it.effect('give back what was saved, and the bridge holds a valid message', () =>
    Effect.gen(function* () {
      const fake = makeFakePreferencesBridge();
      const preferences = yield* over(fake.bridge);
      yield* preferences.save(dark);
      expect(yield* preferences.load).toEqual(dark);
      const held = fake.stored();
      expect(held === null ? null : appearanceFromBytes(held)).toEqual(dark);
    }),
  );

  it.effect('fail naming the action when the file cannot be written or read', () =>
    Effect.gen(function* () {
      const writing = yield* over(makeFakePreferencesBridge({failWrite: () => true}).bridge);
      const written = yield* Effect.flip(writing.save(dark));
      expect(written).toEqual(new PreferencesError({action: 'write', detail: 'disk full'}));
      const reading = yield* over(makeFakePreferencesBridge({failRead: 'denied'}).bridge);
      const read = yield* Effect.flip(reading.load);
      expect(read).toEqual(new PreferencesError({action: 'read', detail: 'denied'}));
    }),
  );

  it.effect('refuse a saved file that is not a valid message instead of showing a guess', () =>
    Effect.gen(function* () {
      const preferences = yield* over(
        makeFakePreferencesBridge({savedBytes: Uint8Array.of(0xff, 0xff)}).bridge,
      );
      const error = yield* Effect.flip(preferences.load);
      expect(error.action).toBe('read');
    }),
  );

  it.effect('fail, rather than pretend, in a window with no bridge', () =>
    Effect.gen(function* () {
      const preferences = yield* over(undefined);
      expect((yield* Effect.flip(preferences.load)).action).toBe('read');
      expect((yield* Effect.flip(preferences.save(dark))).action).toBe('write');
    }),
  );
});
