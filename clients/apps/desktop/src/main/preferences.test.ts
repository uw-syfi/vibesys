import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  DesktopAppearanceSchema,
  DesktopPreferencesSchema,
  DesktopRunRefSchema,
  validatedFromBinary,
} from '@vibesys/proto';
import {APPEARANCE_MODES, type Appearance, themesOf} from '@vibesys/ui/tokens';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as TestClock from 'effect/testing/TestClock';
import fc from 'fast-check';
import {
  appearanceFromBytes,
  appearanceToBytes,
  PREFERENCES_READ_CHANNEL,
  PREFERENCES_WRITE_CHANNEL,
} from '../core/settings/preferences-codec.js';
import {viewerToBytes} from '../core/viewer/viewer-codec.js';
import {directory} from '../testing/main/memory-directory.js';
import {makeMemoryFiles} from '../testing/main/memory-files.js';
import {makeFileLock, makeWriteFile} from './files.js';
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

  it.effect('reads a damaged file as nothing saved, and says so', () =>
    Effect.gen(function* () {
      const {store, logged} = storeOver({[FILE]: Uint8Array.of(0xff, 0xff, 0xff)});
      expect(yield* store.read).toBeNull();
      expect(logged.join()).toContain(FILE);
    }),
  );

  it.effect('never replaces a file it cannot read, whatever the bytes are', () =>
    Effect.gen(function* () {
      const valid = (bytes: Uint8Array) => {
        try {
          validatedFromBinary(DesktopPreferencesSchema, bytes);
          return true;
        } catch {
          return false;
        }
      };
      // A file a newer app wrote: valid wire format, but a version this app does not know.
      const newer = toBinary(
        DesktopPreferencesSchema,
        create(DesktopPreferencesSchema, {
          version: 2,
          appearance: {mode: 1, light: 1, dark: 4},
          notificationsEnabled: false,
          archived: [create(DesktopRunRefSchema, {host: 'local', runId: 'run-a'})],
        }),
      );
      const samples = [
        newer,
        Uint8Array.of(0xff, 0xff, 0xff),
        ...fc.sample(fc.uint8Array({maxLength: 24}), {numRuns: 60, seed: 11}),
      ].filter(bytes => bytes.length > 0 && !valid(bytes));
      expect(samples.length).toBeGreaterThan(10);
      for (const bytes of samples) {
        const {store, files, logged} = storeOver({[FILE]: bytes});
        const error = yield* Effect.flip(store.write(appearanceToBytes(dark)));
        expect(error.message).toContain(FILE);
        expect(files.at(FILE)).toBe(bytes);
        expect(files.written()).toEqual([]);
        expect(logged.join()).toContain(FILE);
      }
    }),
  );

  it.effect('writes back a field only a newer app knows', () =>
    Effect.gen(function* () {
      const known = toBinary(
        DesktopPreferencesSchema,
        create(DesktopPreferencesSchema, {
          version: 1,
          appearance: {mode: 1, light: 1, dark: 4},
          notificationsEnabled: false,
        }),
      );
      // Field 99, varint 5: the tag is 99 << 3 = 792, as the varint 0x98 0x06.
      const extra = Uint8Array.of(0x98, 0x06, 0x05);
      const {store, files} = storeOver({[FILE]: Uint8Array.of(...known, ...extra)});
      yield* store.write(appearanceToBytes(dark));
      const after = stored(files.at(FILE));
      expect(after.notificationsEnabled).toBe(false);
      expect(after.$unknown?.map(field => field.no)).toEqual([99]);
    }),
  );

  it.effect('runs saves one at a time, so a save never reads before the last one has written', () =>
    Effect.gen(function* () {
      const memory = makeMemoryFiles();
      let active = 0;
      let deepest = 0;
      const files = {
        read: (path: string) =>
          Effect.gen(function* () {
            active += 1;
            deepest = Math.max(deepest, active);
            yield* Effect.yieldNow;
            return yield* memory.read(path);
          }),
        write: (path: string, bytes: Uint8Array) =>
          Effect.gen(function* () {
            yield* Effect.yieldNow;
            yield* memory.write(path, bytes);
            active -= 1;
          }),
        lock: memory.lock,
      };
      const store = makePreferencesStore('/data', files, () => undefined);
      yield* Effect.all(
        [appearanceToBytes(dark), appearanceToBytes({...dark, mode: 'light'})].map(store.write),
        {concurrency: 'unbounded'},
      );
      expect(deepest).toBe(1);
    }),
  );

  it.effect('keeps the update of another app instance saving at the same time', () =>
    Effect.gen(function* () {
      // Two instances: each has its own store (and so its own process-wide turn) over one directory.
      const shared = directory();
      // Each read waits a moment for the other's, so without a lock both read the file before either
      // has written; with the lock the other never gets as far as a read, and the wait runs out.
      let readers = 0;
      let tokens = 0;
      const together = Deferred.makeUnsafe<void>();
      const instance = () => {
        const write = makeWriteFile(shared.ops, () => {
          tokens += 1;
          return `t${tokens}`;
        });
        return makePreferencesStore(
          '/data',
          {
            read: path =>
              Effect.gen(function* () {
                readers += 1;
                if (readers === 2) yield* Deferred.succeed(together, undefined);
                yield* Effect.race(Deferred.await(together), Effect.sleep('5 millis'));
                return shared.files.get(path)?.bytes ?? null;
              }),
            write,
            lock: makeFileLock(shared.ops),
          },
          () => undefined,
        );
      };
      const [first, second] = [instance(), instance()];
      const viewer = viewerToBytes({id: 'install-1', label: 'ana'});
      // Not the default, so a save that starts from the defaults shows.
      const chosen: Appearance = {mode: 'light', light: 'sand', dark: 'terminal'};
      const saving = yield* Effect.all(
        [first.write(appearanceToBytes(chosen)), second.writeViewer(viewer)],
        {concurrency: 'unbounded'},
      ).pipe(Effect.forkChild);
      for (let step = 0; step < 40; step++) yield* TestClock.adjust('25 millis');
      yield* Fiber.join(saving);
      const saved = stored(shared.files.get(FILE)?.bytes);
      expect(saved.appearance).toBeDefined();
      if (saved.appearance === undefined) throw new Error('an appearance was saved');
      expect(appearanceFromBytes(toBinary(DesktopAppearanceSchema, saved.appearance))).toEqual(
        chosen,
      );
      expect(saved.viewer?.id).toBe('install-1');
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

  it.effect('is saved over the file as it was, and an appearance saved later keeps it', () =>
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
      yield* store.writeViewer(viewerToBytes({id: 'install-1', label: 'ana'}));
      const after = stored(files.at(FILE));
      expect(after.notificationsEnabled).toBe(false);
      expect(after.archived.map(run => run.runId)).toEqual(['run-a']);
      expect(after.viewer?.id).toBe('install-1');
      yield* store.write(appearanceToBytes(dark));
      expect(stored(files.at(FILE)).viewer?.id).toBe('install-1');
    }),
  );

  it.effect('is never saved over a file this app cannot read, which is left alone', () =>
    Effect.gen(function* () {
      const damaged = Uint8Array.of(0xff, 0xff, 0xff);
      const {store, files, logged} = storeOver({[FILE]: damaged});
      expect(yield* store.readViewer).toBeNull();
      const error = yield* Effect.flip(
        store.writeViewer(viewerToBytes({id: 'install-1', label: 'ana'})),
      );
      expect(error.message).toContain('left as it is');
      expect(files.at(FILE)).toEqual(damaged);
      expect(files.written()).toEqual([]);
      expect(logged.join('\n')).toContain(FILE);
    }),
  );

  it.effect('two saves made at the same moment, a viewer and an appearance, lose neither', () =>
    Effect.gen(function* () {
      const {store, files} = storeOver();
      yield* Effect.all(
        [
          store.writeViewer(viewerToBytes({id: 'install-1', label: 'ana'})),
          store.write(appearanceToBytes(dark)),
        ],
        {concurrency: 'unbounded'},
      );
      const after = stored(files.at(FILE));
      expect(after.viewer?.id).toBe('install-1');
      const bytes = yield* store.read;
      expect(bytes === null ? null : appearanceFromBytes(bytes)).toEqual(dark);
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
