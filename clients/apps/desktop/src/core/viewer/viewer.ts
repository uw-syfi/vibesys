/**
 * Who this install of the app is, as the window reads it: a role with a real implementation over
 * the preload's bridge to the main process, which makes and keeps it, and a Fake in `testing`. The
 * window never makes an id: the main process stamps the same one on every call to a host, so the
 * check-ins a host holds for this viewer are the ones it reads back.
 */
import * as Context from 'effect/Context';
import * as Data from 'effect/Data';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import {type ViewerBridge, type ViewerIdentity, viewerFromBytes} from './viewer-codec.js';

/** The viewer could not be read; `detail` says why. */
export class ViewerError extends Data.TaggedError('ViewerError')<{
  readonly detail: string;
}> {}

export class Viewer extends Context.Service<
  Viewer,
  {
    /** This install: its id and the name it shows. */
    readonly whoami: Effect.Effect<ViewerIdentity, ViewerError>;
  }
>()('app/Viewer') {}

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

/** `Viewer` over the bridge; without one every read fails with `ViewerError`. */
export const viewerOver = (bridge: ViewerBridge | undefined): Layer.Layer<Viewer> =>
  Layer.succeed(
    Viewer,
    Viewer.of({
      whoami: Effect.callback<ViewerIdentity, ViewerError>(resume => {
        const failed = (detail: string) => resume(Effect.fail(new ViewerError({detail})));
        if (bridge === undefined) return failed('the window has no viewer bridge');
        bridge.read(
          bytes => {
            try {
              resume(Effect.succeed(viewerFromBytes(bytes)));
            } catch (cause) {
              failed(describe(cause));
            }
          },
          detail => failed(detail),
        );
      }),
    }),
  );

/** The handle the preload exposes on the window, if it has one, read once when the layer is built. */
export const ViewerFromWindow: Layer.Layer<Viewer> = Layer.unwrap(
  Effect.sync(() => viewerOver(globalThis.vibesysViewer)),
);
