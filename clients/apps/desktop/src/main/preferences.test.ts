import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {DesktopPreferencesSchema, DesktopRunRefSchema, validatedFromBinary} from '@vibesys/proto';
import {APPEARANCE_MODES, type Appearance, themesOf} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import fc from 'fast-check';
import {
  appearanceFromBytes,
  appearanceToBytes,
  PREFERENCES_READ_CHANNEL,
  PREFERENCES_WRITE_CHANNEL,
} from '../core/settings/preferences-codec.js';
import {viewerToBytes} from '../core/viewer/viewer-codec.js';
import {makeMemoryFiles} from '../testing/main/memory-files.js';
import {installPreferencesIpc, makePreferencesStore, type PreferencesIpc} from './preferences.js';

const FILE = '/data/preferences.pb';
const appearance: fc.Arbitrary<Appearance> = fc.record({
  mode: fc.constantFrom(...APPEARANCE_MODES),
  light: fc.constantFrom(...themesOf('light')),
  dark: fc.constantFrom(...themesOf('dark')),
});
const dark: Appearance = {mode: 'dark', light: 'sand', dark: 'terminal'};

const storeOver = (initial: Record<string, Uint8Array> = {}) => {
  const files = makeMemoryFiles(initial);
  const logged: string[] = [];
  return {
    files,
    logged,
    store: makePreferencesStore('/data', files, line => logged.push(line)),
  };
};

const stored = (bytes: Uint8Array | undefined) => {
  if (bytes === undefined) throw new Error('nothing was written');
  return validatedFromBinary(DesktopPreferencesSchema, bytes);
};

describe('the preferences file', () => {
  it.effect('has nothing saved at first', () =>
    Effect.gen(function* () {
      expect(yield* storeOver().store.read).toBeNull();
    }),
  );

  it.effect('gives back any appearance written, in a file that is a valid DesktopPreferences', () =>
    Effect.gen(function* () {
      const samples = fc.sample(appearance, {numRuns: 30, seed: 3});
      for (const chosen of samples) {
        const {store, files} = storeOver();
        yield* store.write(appearanceToBytes(chosen));
        const bytes = yield* store.read;
        expect(bytes === null ? null : appearanceFromBytes(bytes)).toEqual(chosen);
        expect(stored(files.at(FILE)).version).toBe(1);
      }
    }),
  );

  it.effect('keeps the other preferences of the file when it replaces the appearance', () =>
    Effect.gen(function* () {
      const before = toBinary(
        DesktopPreferencesSchema,
        create(DesktopPreferencesSchema, {
          version: 1,
          appearance: {mode: 1, light: 1, dark: 4},
          notificationsEnabled: false,
          archived: [create(DesktopRunRefSchema, {host: 'local', runId: 'run-a'})],
        }),
      );
      const {store, files} = storeOver({[FILE]: before});
      yield* store.write(appearanceToBytes(dark));
      const after = stored(files.at(FILE));
      expect(after.notificationsEnabled).toBe(false);
      expect(after.archived.map(run => run.runId)).toEqual(['run-a']);
      const bytes = yield* store.read;
      expect(bytes === null ? null : appearanceFromBytes(bytes)).toEqual(dark);
    }),
  );

  it.effect(
    'treats a damaged file as nothing saved, says so, and replaces it on the next save',
    () =>
      Effect.gen(function* () {
        const {store, files, logged} = storeOver({[FILE]: Uint8Array.of(0xff, 0xff, 0xff)});
        expect(yield* store.read).toBeNull();
        expect(logged.join()).toContain(FILE);
        yield* store.write(appearanceToBytes(dark));
        expect(stored(files.at(FILE)).appearance).toBeDefined();
      }),
  );

  it.effect('refuses bytes that are not a valid appearance and leaves the file alone', () =>
    Effect.gen(function* () {
      const {store, files} = storeOver();
      const error = yield* Effect.flip(store.write(Uint8Array.of(0xff, 0xff)));
      expect(error).toBeInstanceOf(Error);
      expect(files.written()).toEqual([]);
    }),
  );
});

describe('the viewer in the preferences file', () => {
  it.effect('has none saved at first, and gives back the one written', () =>
    Effect.gen(function* () {
      const {store} = storeOver();
      expect(yield* store.readViewer).toBeNull();
      const viewer = viewerToBytes({id: 'install-1', label: 'ana'});
      yield* store.writeViewer(viewer);
      expect(yield* store.readViewer).toEqual(viewer);
    }),
  );

  it.effect('refuses bytes that are not a valid viewer and leaves the file alone', () =>
    Effect.gen(function* () {
      const {store, files} = storeOver();
      const error = yield* Effect.flip(store.writeViewer(Uint8Array.of(0xff, 0xff)));
      expect(error).toBeInstanceOf(Error);
      expect(files.written()).toEqual([]);
    }),
  );
});

describe('the preferences channels', () => {
  const own = {has: (id: number) => id === 1, isAppUrl: (url: string) => url === 'file:///app'};
  const frame = {processId: 1, routingId: 1, url: 'file:///app'};
  const sender = (id: number) => ({
    id,
    mainFrame: frame,
    once: () => undefined,
    on: () => undefined,
    removeListener: () => undefined,
  });

  const serve = () => {
    const {store} = storeOver();
    const handlers = new Map<string, (event: never, value: unknown) => unknown>();
    const ran: Effect.Effect<unknown, Error>[] = [];
    const ipc: PreferencesIpc = {
      handle: (channel, listener) => handlers.set(channel, listener as never),
    };
    installPreferencesIpc(ipc, {
      store,
      own,
      run: effect => {
        ran.push(effect);
        return Promise.resolve(undefined as never);
      },
    });
    const call = (channel: string, event: object, value?: unknown) => {
      handlers.get(channel)?.(event as never, value);
      const effect = ran.pop();
      if (effect === undefined) throw new Error('the handler ran nothing');
      return effect;
    };
    return call;
  };

  it.effect('serve the app own window and refuse any other sender before touching the file', () =>
    Effect.gen(function* () {
      const call = serve();
      const mine = {sender: sender(1), senderFrame: frame};
      expect(yield* call(PREFERENCES_READ_CHANNEL, mine)).toBeNull();
      yield* call(PREFERENCES_WRITE_CHANNEL, mine, appearanceToBytes(dark));
      const foreign = {sender: sender(2), senderFrame: frame};
      expect((yield* Effect.flip(call(PREFERENCES_READ_CHANNEL, foreign))).message).toMatch(
        /not an app-owned window/,
      );
      expect(
        (yield* Effect.flip(call(PREFERENCES_WRITE_CHANNEL, foreign, appearanceToBytes(dark))))
          .message,
      ).toMatch(/not an app-owned window/);
      const elsewhere = {sender: sender(1), senderFrame: {...frame, url: 'https://example.test/'}};
      expect((yield* Effect.flip(call(PREFERENCES_READ_CHANNEL, elsewhere))).message).toMatch(
        /not an app-owned window/,
      );
    }),
  );

  it.effect('refuse a write that is not bytes', () =>
    Effect.gen(function* () {
      const call = serve();
      const mine = {sender: sender(1), senderFrame: frame};
      const error = yield* Effect.flip(call(PREFERENCES_WRITE_CHANNEL, mine, 'dark'));
      expect(error.message).toMatch(/protobuf bytes/);
    }),
  );
});
