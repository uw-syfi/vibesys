/**
 * The person's preferences, kept in the app's user data as `preferences.pb` (a `DesktopPreferences`
 * message of `proto/vibesys/desktop/v1/settings.proto`). The window reads and replaces the
 * appearance only, as `DesktopAppearance` bytes over two IPC channels; the other fields of the
 * file (notifications, archived runs, and any field a newer app added) belong to other features and
 * are written back as they were. A file that cannot be read or is not a valid message is logged and
 * read as nothing saved, so a damaged file never keeps the window from opening; a save never
 * replaces such a file (the person's other data may be in it, or a newer app wrote it), it fails
 * with the reason and leaves the file alone. Saves run one at a time, so two windows cannot lose
 * each other's update.
 */
import {join} from 'node:path';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  DesktopAppearanceSchema,
  DesktopPreferencesSchema,
  validatedExactFromBinary,
  validatedFromBinary,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Semaphore from 'effect/Semaphore';
import {
  PREFERENCES_READ_CHANNEL,
  PREFERENCES_WRITE_CHANNEL,
} from '../core/settings/preferences-codec.js';
import type {ReadFile, WriteFile} from './files.js';
import {isOwnWindow, type OwnWindows, type PortEvent} from './port-ipc.js';

const FILE = 'preferences.pb';
const VERSION = 1;

/** `compute` as an Effect whose failure is an `Error`: a message that does not validate is one. */
const attempt = <A>(compute: () => A): Effect.Effect<A, Error> =>
  Effect.try({
    try: compute,
    catch: cause => (cause instanceof Error ? cause : new Error(String(cause))),
  });

export interface PreferencesStore {
  /** The saved appearance as `DesktopAppearance` bytes; null when nothing valid is saved. */
  readonly read: Effect.Effect<Uint8Array | null, Error>;
  /** Replaces the appearance, keeping the rest of the file. The bytes must be a valid message. */
  readonly write: (appearance: Uint8Array) => Effect.Effect<void, Error>;
}

export function makePreferencesStore(
  userData: string,
  files: {readonly read: ReadFile; readonly write: WriteFile},
  log: (line: string) => void,
): PreferencesStore {
  const path = join(userData, FILE);
  /** What the file holds: nothing, a message (unknown fields kept), or bytes this app cannot read. */
  const held = Effect.gen(function* () {
    const bytes = yield* files.read(path);
    if (bytes === null) return {kind: 'absent'} as const;
    try {
      return {
        kind: 'present',
        preferences: validatedFromBinary(DesktopPreferencesSchema, bytes),
      } as const;
    } catch (cause) {
      return {kind: 'unreadable', cause: String(cause)} as const;
    }
  });
  /** One save at a time in this process: each is a read-modify-write of the whole file. */
  const saving = Semaphore.makeUnsafe(1);
  return {
    read: Effect.gen(function* () {
      const file = yield* held;
      if (file.kind === 'unreadable')
        log(`${path}: ignoring unreadable preferences: ${file.cause}`);
      return file.kind === 'present' && file.preferences.appearance !== undefined
        ? toBinary(DesktopAppearanceSchema, file.preferences.appearance)
        : null;
    }),
    write: appearance =>
      saving.withPermits(1)(
        Effect.gen(function* () {
          const next = yield* attempt(() =>
            validatedExactFromBinary(DesktopAppearanceSchema, appearance),
          );
          const before = yield* held;
          if (before.kind === 'unreadable') {
            const reason = `${path} cannot be read (${before.cause}); it is left as it is`;
            log(reason);
            return yield* Effect.fail(new Error(reason));
          }
          // Start from the file's own message so every other field, including one only a newer
          // app knows, is written back as it was; a missing file starts from the defaults.
          const bytes = toBinary(
            DesktopPreferencesSchema,
            before.kind === 'present'
              ? {...before.preferences, appearance: next}
              : create(DesktopPreferencesSchema, {
                  version: VERSION,
                  appearance: next,
                  notificationsEnabled: true,
                  archived: [],
                }),
          );
          // Never write what this app could not read back.
          yield* attempt(() => validatedFromBinary(DesktopPreferencesSchema, bytes));
          yield* files.write(path, bytes);
        }),
      ),
  };
}

/** What `installPreferencesIpc` uses of Electron's `ipcMain`. */
export interface PreferencesIpc {
  handle(
    channel: string,
    listener: (event: Pick<PortEvent, 'sender' | 'senderFrame'>, value: unknown) => unknown,
  ): unknown;
}

/**
 * Serves the two channels to the app's own windows only: any other sender is refused before the
 * file is touched.
 */
export function installPreferencesIpc(
  ipc: PreferencesIpc,
  {
    store,
    own,
    run,
  }: {
    readonly store: PreferencesStore;
    readonly own: OwnWindows;
    readonly run: <A>(effect: Effect.Effect<A, Error>) => Promise<A>;
  },
): void {
  const refuse = Effect.fail(new Error('not an app-owned window'));
  ipc.handle(PREFERENCES_READ_CHANNEL, event => run(isOwnWindow(event, own) ? store.read : refuse));
  ipc.handle(PREFERENCES_WRITE_CHANNEL, (event, value) => {
    if (!isOwnWindow(event, own)) return run(refuse);
    if (!(value instanceof Uint8Array))
      return run(Effect.fail(new Error('preferences must be protobuf bytes')));
    return run(store.write(value));
  });
}
