/** A color as `#rrggbb`, or `#rrggbbaa` where a role carries alpha (`grid`). */
export type Hex = `#${string}`;

/** What every theme must define; `THEMES` is total over it, so a missing role is a compile error. */
export const COLOR_ROLES = [
  'bg',
  'panel',
  'panel-2',
  'line',
  'text',
  'muted',
  'accent',
  'ok',
  'warn',
  'err',
  'log-bg',
  'log-text',
  'grid',
] as const;
export type ColorRole = (typeof COLOR_ROLES)[number];

export type ThemeMode = 'light' | 'dark';

/** The closed set of themes. The name is the `data-theme` value. */
export type ThemeName = 'paper' | 'mist' | 'sand' | 'ink' | 'midnight' | 'terminal';

export interface Theme {
  readonly mode: ThemeMode;
  readonly colors: Readonly<Record<ColorRole, Hex>>;
}

/** The theme used for each mode until the user picks another. */
export const DEFAULT_THEMES: Readonly<Record<ThemeMode, ThemeName>> = {
  light: 'paper',
  dark: 'ink',
};

/**
 * The six themes. `grid` is `text` at 6 to 7 % alpha. Four values are darker than the prototype's
 * so that status and accent colors read at 4.5:1 as text (see `CONTRAST_PAIRS`).
 */
export const THEMES: Readonly<Record<ThemeName, Theme>> = {
  paper: {
    mode: 'light',
    colors: {
      bg: '#f7f6f3',
      panel: '#ffffff',
      'panel-2': '#f0eee9',
      line: '#e3e0d8',
      text: '#191817',
      muted: '#64615b',
      accent: '#2f5bea',
      ok: '#167a3a',
      warn: '#925500',
      err: '#b42318',
      'log-bg': '#15161a',
      'log-text': '#d9dde5',
      grid: '#19181711',
    },
  },
  mist: {
    mode: 'light',
    colors: {
      bg: '#f3f5f8',
      panel: '#ffffff',
      'panel-2': '#ebeff4',
      line: '#dbe1e8',
      text: '#0f172a',
      muted: '#566173',
      accent: '#6d28d9',
      ok: '#147a3a',
      warn: '#8f5300',
      err: '#b91c1c',
      'log-bg': '#111522',
      'log-text': '#d6dcea',
      grid: '#0f172a11',
    },
  },
  sand: {
    mode: 'light',
    colors: {
      bg: '#f2eee5',
      panel: '#fbf8f2',
      'panel-2': '#ebe5d8',
      line: '#dcd3c1',
      text: '#29231a',
      muted: '#675d4b',
      accent: '#0e726a',
      ok: '#39721d',
      warn: '#8a5000',
      err: '#a8321c',
      'log-bg': '#1c1913',
      'log-text': '#e6dfcf',
      grid: '#29231a11',
    },
  },
  ink: {
    mode: 'dark',
    colors: {
      bg: '#0a0a0a',
      panel: '#111111',
      'panel-2': '#191919',
      line: '#272727',
      text: '#ededed',
      muted: '#a0a0a0',
      accent: '#4f8cff',
      ok: '#3fb950',
      warn: '#d6a03a',
      err: '#f2665f',
      'log-bg': '#050505',
      'log-text': '#d4d4d4',
      grid: '#ededed11',
    },
  },
  midnight: {
    mode: 'dark',
    colors: {
      bg: '#0a0f1e',
      panel: '#10172c',
      'panel-2': '#161f3a',
      line: '#243055',
      text: '#e6eaf5',
      muted: '#9ba7c5',
      accent: '#9787ff',
      ok: '#4cc38a',
      warn: '#e0aa4f',
      err: '#ff7a85',
      'log-bg': '#060a16',
      'log-text': '#d3daf0',
      grid: '#e6eaf511',
    },
  },
  terminal: {
    mode: 'dark',
    colors: {
      bg: '#050806',
      panel: '#0a110c',
      'panel-2': '#0f1a12',
      line: '#1c2c20',
      text: '#d6f5de',
      muted: '#86a891',
      accent: '#3ddc84',
      ok: '#3ddc84',
      warn: '#e3c35a',
      err: '#ff6b6b',
      'log-bg': '#020403',
      'log-text': '#bdf0cb',
      grid: '#d6f5de11',
    },
  },
};
