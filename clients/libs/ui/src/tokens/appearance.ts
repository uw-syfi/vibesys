import {THEMES, type ThemeMode, type ThemeName} from './themes.js';

/** Which mode the app shows: the system's, or always light or always dark. */
export type AppearanceMode = 'system' | ThemeMode;

/** The user's appearance choice: the mode, and the theme each mode shows. */
export interface Appearance {
  readonly mode: AppearanceMode;
  /** A theme whose mode is light. */
  readonly light: ThemeName;
  /** A theme whose mode is dark. */
  readonly dark: ThemeName;
}

export const APPEARANCE_MODES: readonly AppearanceMode[] = ['system', 'light', 'dark'];

/** The theme shown for `appearance` while the system's appearance is `system`. */
export function effectiveTheme(appearance: Appearance, system: ThemeMode): ThemeName {
  const mode = appearance.mode === 'system' ? system : appearance.mode;
  return mode === 'light' ? appearance.light : appearance.dark;
}

/** The themes of `mode`, in the table's order: a picker's options. */
export function themesOf(mode: ThemeMode): readonly ThemeName[] {
  return (Object.keys(THEMES) as ThemeName[]).filter(name => THEMES[name].mode === mode);
}

function themeOf(value: unknown, mode: ThemeMode, path: string): ThemeName {
  const name = themesOf(mode).find(theme => theme === value);
  if (name === undefined) throw new Error(`${path} must be one of ${themesOf(mode).join(', ')}`);
  return name;
}

/**
 * `value` as an `Appearance`, or an `Error` naming the first offending key under `path`. Unknown
 * keys are refused.
 */
export function parseAppearance(value: unknown, path: string): Appearance {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${path} must be an object`);
  }
  const record = value as Record<string, unknown>;
  const unknown = Object.keys(record).find(key => !['mode', 'light', 'dark'].includes(key));
  if (unknown !== undefined) throw new Error(`${path}.${unknown} is not a known field`);
  const mode = APPEARANCE_MODES.find(each => each === record['mode']);
  if (mode === undefined) {
    throw new Error(`${path}.mode must be one of ${APPEARANCE_MODES.join(', ')}`);
  }
  return {
    mode,
    light: themeOf(record['light'], 'light', `${path}.light`),
    dark: themeOf(record['dark'], 'dark', `${path}.dark`),
  };
}
