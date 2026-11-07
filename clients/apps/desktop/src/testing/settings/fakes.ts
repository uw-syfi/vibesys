/**
 * Doubles for what the appearance reads and writes: the preload's preferences bridge, the system's
 * light or dark, and the page the theme is shown on. Each holds the facts a test asserts on.
 */

import type {Appearance, ThemeMode, ThemeName} from '@vibesys/ui/tokens';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import type {PreferencesBridge} from '../../core/settings/preferences-codec.js';
import {appearanceToBytes} from '../../core/settings/preferences-codec.js';
import {SystemAppearance} from '../../core/settings/system-appearance.js';
import {ThemeDocument} from '../../core/settings/theme-document.js';

export interface FakePreferencesBridge {
  readonly bridge: PreferencesBridge;
  /** The bytes held now, or null before anything is saved. */
  readonly stored: () => Uint8Array | null;
  /** How many writes were answered, successful or not. */
  readonly writes: () => number;
}

export interface FakePreferencesOptions {
  /** What is saved before the test starts. */
  readonly saved?: Appearance;
  /** Bytes held as they are, for a file that is not a valid message. */
  readonly savedBytes?: Uint8Array;
  /** Whether the nth write (from 0) fails. */
  readonly failWrite?: (nth: number) => boolean;
  /** Reads fail with this text. */
  readonly failRead?: string;
}

/** The bridge, answering at once, as a window whose main process holds the file in memory. */
export function makeFakePreferencesBridge(
  options: FakePreferencesOptions = {},
): FakePreferencesBridge {
  let stored: Uint8Array | null =
    options.savedBytes ?? (options.saved === undefined ? null : appearanceToBytes(options.saved));
  let writes = 0;
  return {
    bridge: {
      read(done, failed) {
        if (options.failRead === undefined) done(stored);
        else failed(options.failRead);
      },
      write(bytes, done, failed) {
        const nth = writes;
        writes += 1;
        if (options.failWrite?.(nth) === true) failed('disk full');
        else {
          stored = bytes;
          done();
        }
      },
    },
    stored: () => stored,
    writes: () => writes,
  };
}

export interface FakeSystemAppearance {
  readonly layer: Layer.Layer<SystemAppearance>;
  /** The system now asks for `mode`. */
  readonly set: (mode: ThemeMode) => Effect.Effect<void>;
}

export const makeFakeSystemAppearance = (initial: ThemeMode): Effect.Effect<FakeSystemAppearance> =>
  Effect.gen(function* () {
    const queue = yield* Queue.unbounded<ThemeMode>();
    let current = initial;
    return {
      layer: Layer.succeed(
        SystemAppearance,
        SystemAppearance.of({
          current: Effect.sync(() => current),
          changes: Stream.fromQueue(queue),
        }),
      ),
      set: mode =>
        Effect.suspend(() => {
          current = mode;
          return Queue.offer(queue, mode).pipe(Effect.asVoid);
        }),
    };
  });

export interface FakeThemeDocument {
  readonly layer: Layer.Layer<ThemeDocument>;
  /** Every theme the page was shown in, in order. */
  readonly shown: () => readonly ThemeName[];
}

export function makeFakeThemeDocument(): FakeThemeDocument {
  const shown: ThemeName[] = [];
  return {
    layer: Layer.succeed(
      ThemeDocument,
      ThemeDocument.of({
        show: theme =>
          Effect.sync(() => {
            shown.push(theme);
          }),
      }),
    ),
    shown: () => shown,
  };
}
