/**
 * What the window and the main process exchange to keep the person's preferences: a
 * `DesktopAppearance` message of `proto/vibesys/desktop/v1/settings.proto`, as validated protobuf
 * bytes both ways. The themes and the modes are named by the generated enums and by the design
 * system's token table; the names below are derived from them, so a theme added to both is carried
 * with no change here (a test holds the two to each other).
 */
import {create, toBinary} from '@bufbuild/protobuf';
import {
  DesktopAppearanceMode,
  DesktopAppearanceSchema,
  DesktopTheme,
  validatedExactFromBinary,
} from '@vibesys/proto';
import {type Appearance, parseAppearance} from '@vibesys/ui/tokens';

/** The IPC channels the main process serves; the preload repeats them (a test holds the two equal). */
export const PREFERENCES_READ_CHANNEL = 'vibesys:preferences:read';
export const PREFERENCES_WRITE_CHANNEL = 'vibesys:preferences:write';

/**
 * What the preload exposes on the window: the saved appearance as bytes, and a way to replace it.
 * Each call ends in exactly one of its two callbacks. `read` gives `null` when nothing was saved.
 */
export interface PreferencesBridge {
  read(done: (bytes: Uint8Array | null) => void, failed: (error: string) => void): void;
  write(bytes: Uint8Array, done: () => void, failed: (error: string) => void): void;
}

/** The enum value named like `name` (`paper` is `PAPER`), or a refusal naming the enum. */
function enumValue(values: Record<string, string | number>, name: string, what: string): number {
  const value = values[name.toUpperCase()];
  if (typeof value !== 'number')
    throw new Error(`${what} ${JSON.stringify(name)} has no enum value`);
  return value;
}

/** The lowercase name of an enum value (`PAPER` is `paper`). */
function enumName(values: Record<string, string | number>, value: number): string {
  const name = values[value];
  return typeof name === 'string' ? name.toLowerCase() : String(value);
}

/** `appearance` as validated message bytes; a theme the enum lacks is an error naming it. */
export function appearanceToBytes(appearance: Appearance): Uint8Array {
  const bytes = toBinary(
    DesktopAppearanceSchema,
    create(DesktopAppearanceSchema, {
      mode: enumValue(DesktopAppearanceMode, appearance.mode, 'mode'),
      light: enumValue(DesktopTheme, appearance.light, 'theme'),
      dark: enumValue(DesktopTheme, appearance.dark, 'theme'),
    }),
  );
  validatedExactFromBinary(DesktopAppearanceSchema, bytes);
  return bytes;
}

/** The appearance in `bytes`; bytes that are not a valid message, or name an unknown theme, are an error. */
export function appearanceFromBytes(bytes: Uint8Array): Appearance {
  const message = validatedExactFromBinary(DesktopAppearanceSchema, bytes);
  return parseAppearance(
    {
      mode: enumName(DesktopAppearanceMode, message.mode),
      light: enumName(DesktopTheme, message.light),
      dark: enumName(DesktopTheme, message.dark),
    },
    'appearance',
  );
}
