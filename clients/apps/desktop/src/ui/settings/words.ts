/** What the settings screen says. Each closed set is named here, so a value added to one cannot reach the screen unnamed. */
import type {AppearanceMode, ThemeName} from '@vibesys/ui/tokens';

export const MODE_WORD: Readonly<Record<AppearanceMode, string>> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};

/** A theme's name is its token name with a capital: a theme added to the token table is named with no change here. */
export const themeWord = (theme: ThemeName): string =>
  `${theme.charAt(0).toUpperCase()}${theme.slice(1)}`;
