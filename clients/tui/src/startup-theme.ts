import {type ProtocolResponse, TuiTheme} from '@vibesys/backend-client';
import {enumWord} from './ui/enum-word.js';
import {resolveTheme, type ThemeName} from './ui/theme.js';

export interface StartupThemeOptions {
  /** `--theme` from the launcher; when set the backend answer is ignored. */
  explicitTheme?: string | undefined;
  /** Ceiling on waiting for the backend before painting a default theme. */
  timeoutMs?: number;
}

/**
 * A hung backend must still reach the failure UI, so the wait is bounded.
 * The request itself is cheap: the backend resolves it from configuration.
 */
const DEFAULT_DEFAULTS_TIMEOUT_MS = 2_000;

/**
 * Resolve the theme the first frame is painted with.
 *
 * Configuration lives in the backend, so an implicit theme costs one control
 * request. Callers start that request before the renderer so the round trip
 * overlaps renderer startup, then await this before the first frame: the
 * session cannot show run data before it connects anyway, and resolving
 * afterwards would paint one theme and repaint in another.
 *
 * The theme is a presentation preference, so a rejected request, an unknown
 * name, or a timeout all resolve to the built-in default rather than keeping
 * the session from starting.
 */
export async function resolveStartupTheme(
  pending: Promise<ProtocolResponse> | undefined,
  options: StartupThemeOptions = {},
): Promise<ThemeName> {
  if (options.explicitTheme !== undefined) return resolveTheme(options.explicitTheme).name;
  if (pending === undefined) return resolveTheme(undefined).name;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expiry = new Promise<undefined>(resolve => {
    timer = setTimeout(() => resolve(undefined), options.timeoutMs ?? DEFAULT_DEFAULTS_TIMEOUT_MS);
  });
  try {
    const response = await Promise.race([pending.catch(() => undefined), expiry]);
    return resolveTheme(enumWord(TuiTheme, response?.tuiDefaults?.theme)?.replaceAll('_', '-'))
      .name;
  } finally {
    if (timer) clearTimeout(timer);
  }
}
