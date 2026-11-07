/**
 * A double for the preload's viewer bridge: the install of the app as a window whose main process
 * holds it, answering at once. It holds the bytes a test asserts on.
 */
import {
  type ViewerBridge,
  type ViewerIdentity,
  viewerToBytes,
} from '../../core/viewer/viewer-codec.js';

export interface FakeViewerBridge {
  readonly bridge: ViewerBridge;
  /** How many reads were answered, successful or not. */
  readonly reads: () => number;
}

export interface FakeViewerOptions {
  /** Who this install is. */
  readonly viewer?: ViewerIdentity;
  /** Bytes served as they are, for a main process that sends something that is not a valid message. */
  readonly bytes?: Uint8Array;
  /** Reads fail with this text. */
  readonly failRead?: string;
}

export function makeFakeViewerBridge(options: FakeViewerOptions = {}): FakeViewerBridge {
  let reads = 0;
  return {
    bridge: {
      read(done, failed) {
        reads += 1;
        if (options.failRead !== undefined) failed(options.failRead);
        else if (options.bytes !== undefined) done(options.bytes);
        else done(viewerToBytes(options.viewer ?? {id: 'install-ana', label: 'ana'}));
      },
    },
    reads: () => reads,
  };
}
