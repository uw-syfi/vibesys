/**
 * Where the person's appearance is kept between runs of the app: a role with a real implementation
 * over the preload's bridge to the main process, which owns the file, and a Fake in `testing`.
 */

import type {Appearance} from '@vibesys/ui/tokens';
import * as Context from 'effect/Context';
import * as Data from 'effect/Data';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import {
  appearanceFromBytes,
  appearanceToBytes,
  type PreferencesBridge,
} from './preferences-codec.js';

/** The saved appearance could not be read or written; `detail` says why. */
export class PreferencesError extends Data.TaggedError('PreferencesError')<{
  readonly action: 'read' | 'write';
  readonly detail: string;
}> {}

export class Preferences extends Context.Service<
  Preferences,
  {
    /** What was saved: `null` when nothing was. */
    readonly load: Effect.Effect<Appearance | null, PreferencesError>;
    /** Replaces what is saved. */
    readonly save: (appearance: Appearance) => Effect.Effect<void, PreferencesError>;
  }
>()('app/Preferences') {}

const describe = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

const NO_BRIDGE = 'the window has no preferences bridge';

/** `Preferences` over the bridge; without one every call fails with `PreferencesError`. */
export const preferencesOver = (bridge: PreferencesBridge | undefined): Layer.Layer<Preferences> =>
  Layer.succeed(
    Preferences,
    Preferences.of({
      load: Effect.callback<Appearance | null, PreferencesError>(resume => {
        const failed = (detail: string) =>
          resume(Effect.fail(new PreferencesError({action: 'read', detail})));
        if (bridge === undefined) return failed(NO_BRIDGE);
        bridge.read(
          bytes => {
            try {
              resume(Effect.succeed(bytes === null ? null : appearanceFromBytes(bytes)));
            } catch (cause) {
              failed(describe(cause));
            }
          },
          detail => failed(detail),
        );
      }),
      save: appearance =>
        Effect.callback<void, PreferencesError>(resume => {
          const failed = (detail: string) =>
            resume(Effect.fail(new PreferencesError({action: 'write', detail})));
          if (bridge === undefined) return failed(NO_BRIDGE);
          let bytes: Uint8Array;
          try {
            bytes = appearanceToBytes(appearance);
          } catch (cause) {
            return failed(describe(cause));
          }
          bridge.write(
            bytes,
            () => resume(Effect.void),
            detail => failed(detail),
          );
        }),
    }),
  );

/** The handle the preload exposes on the window, if it has one, read once when the layer is built. */
export const PreferencesFromWindow: Layer.Layer<Preferences> = Layer.unwrap(
  Effect.sync(() => preferencesOver(globalThis.vibesysPreferences)),
);
