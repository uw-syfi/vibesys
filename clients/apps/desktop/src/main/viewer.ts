/**
 * Who this install of the app is: an id made once and kept in the preferences file, and a name to
 * show (the operating system's user name until the person changes it). The main process stamps both
 * on every call to a host as metadata (`state/viewer.ts`), where the host keeps a viewer's
 * check-ins for Catch me up, and serves them read-only to the window, which needs the id to tell
 * which check-ins are its own. Nothing authenticates the viewer and it grants nothing.
 *
 * It is read once per run of the app. An app that cannot save it (a damaged or unwritable file,
 * logged) still has the one it made for this run, so calls are never made without it; the next
 * run makes another.
 */
import * as Effect from 'effect/Effect';
import {
  VIEWER_READ_CHANNEL,
  type ViewerIdentity,
  viewerFromBytes,
  viewerToBytes,
} from '../core/viewer/viewer-codec.js';
import {labelFromName, viewerMetadata} from '../state/index.js';
import {isOwnWindow, type OwnWindows, type PortEvent} from './port-ipc.js';
import type {PreferencesStore} from './preferences.js';

/** What the viewer is made from: a fresh id, and the name of the person at this machine. */
export interface ViewerSource {
  readonly newId: () => string;
  readonly userName: () => string;
}

export interface ViewerStore {
  /** This install, the same for every call of a run. Never fails. */
  readonly viewer: Effect.Effect<ViewerIdentity>;
  /** The metadata every call to a host carries. */
  readonly metadata: Effect.Effect<Readonly<Record<string, string>>>;
}

/** What the viewer needs of the preferences file: its own field. */
type ViewerFile = Pick<PreferencesStore, 'readViewer' | 'writeViewer'>;

const said = (cause: Error): string => cause.message;

export const makeViewerStore = (
  file: ViewerFile,
  source: ViewerSource,
  log: (line: string) => void,
): Effect.Effect<ViewerStore> =>
  Effect.gen(function* () {
    const load = Effect.gen(function* () {
      const saved = yield* file.readViewer.pipe(
        Effect.catch(cause =>
          Effect.sync(() => {
            log(`could not read the viewer: ${said(cause)}`);
            return null;
          }),
        ),
      );
      if (saved !== null) {
        try {
          return viewerFromBytes(saved);
        } catch (cause) {
          log(`ignoring an unreadable viewer: ${String(cause)}`);
        }
      }
      const made: ViewerIdentity = {id: source.newId(), label: labelFromName(source.userName())};
      yield* file
        .writeViewer(viewerToBytes(made))
        .pipe(
          Effect.catch(cause =>
            Effect.sync(() =>
              log(`could not save the viewer, so it lasts for this run: ${said(cause)}`),
            ),
          ),
        );
      return made;
    });
    const viewer = yield* Effect.cached(load);
    return {viewer, metadata: Effect.map(viewer, viewerMetadata)};
  });

/** What `installViewerIpc` uses of Electron's `ipcMain`. */
export interface ViewerIpc {
  handle(
    channel: string,
    listener: (event: Pick<PortEvent, 'sender' | 'senderFrame'>) => unknown,
  ): unknown;
}

/** Serves the viewer to the app's own windows only: any other sender is refused before it is read. */
export function installViewerIpc(
  ipc: ViewerIpc,
  {
    store,
    own,
    run,
  }: {
    readonly store: ViewerStore;
    readonly own: OwnWindows;
    readonly run: <A>(effect: Effect.Effect<A, Error>) => Promise<A>;
  },
): void {
  const refuse = Effect.fail(new Error('not an app-owned window'));
  ipc.handle(VIEWER_READ_CHANNEL, event =>
    run(isOwnWindow(event, own) ? Effect.map(store.viewer, viewerToBytes) : refuse),
  );
}
