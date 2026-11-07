import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {DesktopPreferencesSchema, DesktopRunRefSchema, validatedFromBinary} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import fc from 'fast-check';
import {appearanceFromBytes, appearanceToBytes} from '../core/settings/preferences-codec.js';
import {VIEWER_READ_CHANNEL, viewerFromBytes, viewerToBytes} from '../core/viewer/viewer-codec.js';
import {defaultAppearance} from '../state/index.js';
import {makeMemoryFiles} from '../testing/main/memory-files.js';
import {makePreferencesStore, type PreferencesStore} from './preferences.js';
import {installViewerIpc, makeViewerStore, type ViewerIpc} from './viewer.js';

const FILE = '/data/preferences.pb';

/** The preferences file in memory, the viewer store over it, and what was logged. */
const over = (initial: Record<string, Uint8Array> = {}, ids = ['install-1', 'install-2']) => {
  const files = makeMemoryFiles(initial);
  const logged: string[] = [];
  const preferences = makePreferencesStore('/data', files, line => logged.push(line));
  const issued = [...ids];
  const made = (file: Pick<PreferencesStore, 'readViewer' | 'writeViewer'> = preferences) =>
    makeViewerStore(
      file,
      {newId: () => issued.shift() ?? 'install-out', userName: () => 'ana'},
      line => logged.push(line),
    );
  return {files, logged, preferences, made};
};

const stored = (bytes: Uint8Array | undefined) => {
  if (bytes === undefined) throw new Error('nothing was written');
  return validatedFromBinary(DesktopPreferencesSchema, bytes);
};

describe('the viewer of this install', () => {
  it.effect('is made once, from a fresh id and the name of the user, and saved', () =>
    Effect.gen(function* () {
      const {made, files} = over();
      const store = yield* made();
      expect(yield* store.viewer).toEqual({id: 'install-1', label: 'ana'});
      expect(stored(files.at(FILE)).viewer).toMatchObject({id: 'install-1', label: 'ana'});
      // The same one every time it is asked, with no second write.
      expect(yield* store.viewer).toEqual({id: 'install-1', label: 'ana'});
      expect(files.written()).toEqual([FILE]);
    }),
  );

  it.effect('is the same one in the next run of the app, whatever the id source says then', () =>
    Effect.gen(function* () {
      const {made, files} = over();
      const first = yield* (yield* made()).viewer;
      const again = makeViewerStore(
        makePreferencesStore('/data', files, () => undefined),
        {newId: () => 'someone-else', userName: () => 'bob'},
        () => undefined,
      );
      expect(yield* (yield* again).viewer).toEqual(first);
    }),
  );

  it.effect(
    'says who is calling in the metadata of every call: both fields, the same each time',
    () =>
      Effect.gen(function* () {
        const {made} = over();
        const store = yield* made();
        const expected = {'vibesys-viewer-id': 'install-1', 'vibesys-viewer-label': 'ana'};
        expect(yield* store.metadata).toEqual(expected);
        expect(yield* store.metadata).toEqual(expected);
      }),
  );

  it.effect('makes a label a header can carry from any name, and one the contract accepts', () =>
    Effect.gen(function* () {
      for (const name of fc.sample(fc.string({unit: 'binary', maxLength: 200}), {
        numRuns: 40,
        seed: 5,
      })) {
        const files = makeMemoryFiles();
        const store = yield* makeViewerStore(
          makePreferencesStore('/data', files, () => undefined),
          {newId: () => 'install-1', userName: () => name},
          () => undefined,
        );
        const {label} = yield* store.viewer;
        expect(label).toMatch(/^[!-~]([ -~]{0,126}[!-~])?$/);
        // What was saved is a message the contract accepts and reads back as it was made.
        expect(stored(files.at(FILE)).viewer?.label).toBe(label);
      }
    }),
  );

  it.effect('keeps the other preferences of the file, and the appearance a person chose', () =>
    Effect.gen(function* () {
      const before = toBinary(
        DesktopPreferencesSchema,
        create(DesktopPreferencesSchema, {
          version: 1,
          appearance: {mode: 2, light: 3, dark: 4},
          notificationsEnabled: false,
          archived: [create(DesktopRunRefSchema, {host: 'local', runId: 'run-a'})],
        }),
      );
      const {made, files, preferences} = over({[FILE]: before});
      yield* (yield* made()).viewer;
      const after = stored(files.at(FILE));
      expect(after.notificationsEnabled).toBe(false);
      expect(after.archived.map(run => run.runId)).toEqual(['run-a']);
      expect(after.appearance).toMatchObject({mode: 2, light: 3, dark: 4});
      expect(after.viewer?.id).toBe('install-1');
      // And a later change of the appearance keeps the viewer.
      yield* preferences.write(appearanceToBytes({mode: 'dark', light: 'sand', dark: 'terminal'}));
      expect(stored(files.at(FILE)).viewer?.id).toBe('install-1');
    }),
  );

  it.effect('leaves the appearance what it was when the file did not exist: the default', () =>
    Effect.gen(function* () {
      const {made, preferences} = over();
      yield* (yield* made()).viewer;
      const bytes = yield* preferences.read;
      expect(bytes === null ? null : appearanceFromBytes(bytes)).toEqual(defaultAppearance);
    }),
  );

  it.effect('lasts for the run, and leaves a damaged file as it is, so no data in it is lost', () =>
    Effect.gen(function* () {
      const damaged = Uint8Array.of(0xff, 0xff, 0xff);
      const {made, files, logged} = over({[FILE]: damaged});
      const store = yield* made();
      expect(yield* store.viewer).toEqual({id: 'install-1', label: 'ana'});
      expect(yield* store.viewer).toEqual({id: 'install-1', label: 'ana'});
      expect(files.at(FILE)).toEqual(damaged);
      expect(files.written()).toEqual([]);
      expect(logged.join('\n')).toContain(FILE);
    }),
  );

  it.effect('lasts for the run, with the reason logged, when it cannot be saved', () =>
    Effect.gen(function* () {
      const {made, logged} = over();
      const store = yield* made({
        readViewer: Effect.succeed(null),
        writeViewer: () => Effect.fail(new Error('disk full')),
      });
      const first = yield* store.viewer;
      expect(first).toEqual({id: 'install-1', label: 'ana'});
      expect(yield* store.viewer).toEqual(first);
      expect(logged.join('\n')).toContain('disk full');
    }),
  );

  it.effect('does not fail when the file cannot be read: it makes one and goes on', () =>
    Effect.gen(function* () {
      const {made, logged} = over();
      const store = yield* made({
        readViewer: Effect.fail(new Error('permission denied')),
        writeViewer: () => Effect.void,
      });
      expect((yield* store.viewer).id).toBe('install-1');
      expect(logged.join('\n')).toContain('permission denied');
    }),
  );
});

describe('the viewer channel', () => {
  const own = {has: (id: number) => id === 1, isAppUrl: (url: string) => url === 'file:///app'};
  const frame = {processId: 1, routingId: 1, url: 'file:///app'};
  const sender = (id: number) => ({
    id,
    mainFrame: frame,
    once: () => undefined,
    on: () => undefined,
    removeListener: () => undefined,
  });

  const serve = Effect.gen(function* () {
    const {made} = over();
    const store = yield* made();
    const handlers = new Map<string, (event: never) => unknown>();
    const ran: Effect.Effect<unknown, Error>[] = [];
    const ipc: ViewerIpc = {
      handle: (channel, listener) => handlers.set(channel, listener as never),
    };
    installViewerIpc(ipc, {
      store,
      own,
      run: effect => {
        ran.push(effect);
        return Promise.resolve(undefined as never);
      },
    });
    return (event: object) => {
      handlers.get(VIEWER_READ_CHANNEL)?.(event as never);
      const effect = ran.pop();
      if (effect === undefined) throw new Error('the handler ran nothing');
      return effect;
    };
  });

  it.effect('serves the viewer to the app own window and refuses any other sender', () =>
    Effect.gen(function* () {
      const call = yield* serve;
      const bytes = yield* call({sender: sender(1), senderFrame: frame});
      expect(viewerFromBytes(bytes as Uint8Array)).toEqual({id: 'install-1', label: 'ana'});
      expect(bytes).toEqual(viewerToBytes({id: 'install-1', label: 'ana'}));
      for (const event of [
        {sender: sender(2), senderFrame: frame},
        {sender: sender(1), senderFrame: {...frame, url: 'https://example.test/'}},
        {sender: sender(1), senderFrame: null},
      ]) {
        expect((yield* Effect.flip(call(event))).message).toMatch(/not an app-owned window/);
      }
    }),
  );
});
