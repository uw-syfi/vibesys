/**
 * What the window and the main process exchange to say who this install of the app is: a
 * `DesktopViewer` message of `proto/vibesys/desktop/v1/settings.proto`, as validated protobuf bytes.
 * The main process owns it (it makes the id once, keeps it in the preferences file and stamps it on
 * every call to a host); the window only reads it, to tell which check-ins a host keeps are its own.
 */
import {create, toBinary} from '@bufbuild/protobuf';
import {DesktopViewerSchema, validatedExactFromBinary} from '@vibesys/proto';
import type {Viewer} from '../../state/index.js';

/** The IPC channel the main process serves; the preload repeats it (a test holds the two equal). */
export const VIEWER_READ_CHANNEL = 'vibesys:viewer:read';

/** An install of the app and the name it shows. */
export type ViewerIdentity = Viewer;

/**
 * What the preload exposes on the window: the viewer as bytes. The call ends in exactly one of its
 * two callbacks.
 */
export interface ViewerBridge {
  read(done: (bytes: Uint8Array) => void, failed: (error: string) => void): void;
}

/** `viewer` as validated message bytes; an id or a label the contract refuses is an error. */
export function viewerToBytes(viewer: ViewerIdentity): Uint8Array {
  const bytes = toBinary(DesktopViewerSchema, create(DesktopViewerSchema, viewer));
  validatedExactFromBinary(DesktopViewerSchema, bytes);
  return bytes;
}

/** The viewer in `bytes`; bytes that are not a valid message are an error. */
export function viewerFromBytes(bytes: Uint8Array): ViewerIdentity {
  const {id, label} = validatedExactFromBinary(DesktopViewerSchema, bytes);
  return {id, label};
}
