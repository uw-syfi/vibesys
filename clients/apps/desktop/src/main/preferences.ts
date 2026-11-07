/**
 * The person's preferences, kept in the app's user data as `preferences.pb` (a `DesktopPreferences`
 * message of `proto/vibesys/desktop/v1/settings.proto`). The window reads and replaces the
 * appearance only, as `DesktopAppearance` bytes over two IPC channels; the other fields of the
 * file (notifications, archived runs) belong to other features and are kept as they are. The viewer
 * (who this install is to a host, `viewer.ts`) is one of them: the main process alone reads and
 * replaces it. A file that cannot be read or is not a valid message is logged and treated as
 * absent, so a damaged file never keeps the window from opening, and the next save replaces it.
 */
import {join} from 'node:path';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  DesktopAppearanceSchema,
  DesktopPreferencesSchema,
  DesktopViewerSchema,
  validatedExactFromBinary,
  validatedFromBinary,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import {
  appearanceToBytes,
  PREFERENCES_READ_CHANNEL,
  PREFERENCES_WRITE_CHANNEL,
} from '../core/settings/preferences-codec.js';
import {defaultAppearance} from '../state/index.js';
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
  /** The saved viewer as `DesktopViewer` bytes; null when none is saved. */
  readonly readViewer: Effect.Effect<Uint8Array | null, Error>;
  /**
   * Replaces the viewer, keeping the rest of the file. The bytes must be a valid message. A file
   * that does not exist yet holds the default appearance beside it, which is what the window
   * shows when nothing is saved, so saving the viewer first changes nothing the person sees.
   */
  readonly writeViewer: (viewer: Uint8Array) => Effect.Effect<void, Error>;
}

export function makePreferencesStore(
  userData: string,
  files: {readonly read: ReadFile; readonly write: WriteFile},
  log: (line: string) => void,
): PreferencesStore {
  const path = join(userData, FILE);
  /** What the file holds, or undefined when it is absent or damaged (logged). */
  const held = Effect.gen(function* () {
    const bytes = yield* files.read(path);
    if (bytes === null) return undefined;
    try {
      return validatedFromBinary(DesktopPreferencesSchema, bytes);
    } catch (cause) {
      log(`${path}: ignoring unreadable preferences: ${String(cause)}`);
      return undefined;
    }
  });
  return {
    read: Effect.map(held, preferences =>
      preferences?.appearance === undefined
        ? null
        : toBinary(DesktopAppearanceSchema, preferences.appearance),
    ),
    write: appearance =>
      Effect.gen(function* () {
        const next = yield* attempt(() =>
          validatedExactFromBinary(DesktopAppearanceSchema, appearance),
        );
        const before = yield* held;
        const bytes = toBinary(
          DesktopPreferencesSchema,
          create(DesktopPreferencesSchema, {
            version: VERSION,
            appearance: next,
            notificationsEnabled: before?.notificationsEnabled ?? true,
            archived: before?.archived ?? [],
            ...(before?.viewer === undefined ? {} : {viewer: before.viewer}),
          }),
        );
        // Never write what this app could not read back.
        yield* attempt(() => validatedFromBinary(DesktopPreferencesSchema, bytes));
        yield* files.write(path, bytes);
      }),
    readViewer: Effect.map(held, preferences =>
      preferences?.viewer === undefined ? null : toBinary(DesktopViewerSchema, preferences.viewer),
    ),
    writeViewer: viewer =>
      Effect.gen(function* () {
        const next = yield* attempt(() => validatedExactFromBinary(DesktopViewerSchema, viewer));
        const before = yield* held;
        const appearance =
          before?.appearance ??
          (yield* attempt(() =>
            validatedExactFromBinary(DesktopAppearanceSchema, appearanceToBytes(defaultAppearance)),
          ));
        const bytes = toBinary(
          DesktopPreferencesSchema,
          create(DesktopPreferencesSchema, {
            version: VERSION,
            appearance,
            notificationsEnabled: before?.notificationsEnabled ?? true,
            archived: before?.archived ?? [],
            viewer: next,
          }),
        );
        yield* attempt(() => validatedFromBinary(DesktopPreferencesSchema, bytes));
        yield* files.write(path, bytes);
      }),
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
