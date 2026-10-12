/** Web token names deliberately match the server's TUI theme names. The browser owns only
 * its token set, not the TUI renderer or its implementation details. */

import type {TuiDefaults} from '@vibesys/contracts';

export type WebThemeName = TuiDefaults['theme'];

const WEB_THEME_NAME_VALUES = [
  'dark',
  'light',
  'solarized-dark',
  'solarized-light',
  'catppuccin-mocha',
  'catppuccin-latte',
  'high-contrast-dark',
  'high-contrast-light',
] as const satisfies readonly WebThemeName[];

type ExhaustiveThemeNames<Names extends readonly WebThemeName[]> =
  Exclude<WebThemeName, Names[number]> extends never ? Names : never;

/** Runtime order for the generated protocol's exhaustive theme union. */
export const WEB_THEME_NAMES: ExhaustiveThemeNames<typeof WEB_THEME_NAME_VALUES> =
  WEB_THEME_NAME_VALUES;

export interface WebThemeTokens {
  readonly pageBackground: string;
  readonly surface: string;
  readonly border: string;
  readonly text: string;
  readonly subtle: string;
  readonly accent: string;
  readonly onAccent: string;
  readonly success: string;
  readonly failure: string;
  readonly onFailure: string;
  readonly warning: string;
  readonly warningSurface: string;
  readonly fieldBackground: string;
}

/** Browser colors are one closed palette per advertised server theme. Components consume only
 * these semantic tokens, so light and high-contrast themes cannot inherit a dark component fill. */
export const WEB_THEME_TOKENS: Readonly<Record<WebThemeName, WebThemeTokens>> = {
  dark: {
    pageBackground: '#0c1018',
    surface: '#131a26',
    border: '#596b83',
    text: '#e9edf5',
    subtle: '#a9b8d0',
    accent: '#79a8ff',
    onAccent: '#07101f',
    success: '#70e0ae',
    failure: '#ff99a8',
    onFailure: '#25050b',
    warning: '#ffc2a8',
    warningSurface: '#34231f',
    fieldBackground: '#111a2d',
  },
  light: {
    pageBackground: '#f7f8fc',
    surface: '#ffffff',
    border: '#7d899a',
    text: '#172033',
    subtle: '#526177',
    accent: '#245fbb',
    onAccent: '#ffffff',
    success: '#18734b',
    failure: '#a5203b',
    onFailure: '#ffffff',
    warning: '#7a3e00',
    warningSurface: '#fff1dc',
    fieldBackground: '#f7f8fc',
  },
  'solarized-dark': {
    pageBackground: '#002b36',
    surface: '#073642',
    border: '#839496',
    text: '#eee8d5',
    subtle: '#b2c1c1',
    accent: '#8ac7e8',
    onAccent: '#002b36',
    success: '#c0d858',
    failure: '#ff9b94',
    onFailure: '#002b36',
    warning: '#f5c451',
    warningSurface: '#3b3420',
    fieldBackground: '#002b36',
  },
  'solarized-light': {
    pageBackground: '#fdf6e3',
    surface: '#eee8d5',
    border: '#657b83',
    text: '#002b36',
    subtle: '#4f6269',
    accent: '#006d9c',
    onAccent: '#ffffff',
    success: '#587000',
    failure: '#a51f1f',
    onFailure: '#ffffff',
    warning: '#704500',
    warningSurface: '#fff2c7',
    fieldBackground: '#fdf6e3',
  },
  'catppuccin-mocha': {
    pageBackground: '#1e1e2e',
    surface: '#313244',
    border: '#7f849c',
    text: '#cdd6f4',
    subtle: '#bac2de',
    accent: '#89b4fa',
    onAccent: '#11111b',
    success: '#a6e3a1',
    failure: '#f38ba8',
    onFailure: '#11111b',
    warning: '#f9e2af',
    warningSurface: '#3b3440',
    fieldBackground: '#181825',
  },
  'catppuccin-latte': {
    pageBackground: '#eff1f5',
    surface: '#e6e9ef',
    border: '#7c7f93',
    text: '#4c4f69',
    subtle: '#5c5f77',
    accent: '#1d63ed',
    onAccent: '#ffffff',
    success: '#1f6b50',
    failure: '#c20d36',
    onFailure: '#ffffff',
    warning: '#784000',
    warningSurface: '#f7e8ca',
    fieldBackground: '#eff1f5',
  },
  'high-contrast-dark': {
    pageBackground: '#000000',
    surface: '#080808',
    border: '#ffffff',
    text: '#ffffff',
    subtle: '#ffffff',
    accent: '#7db7ff',
    onAccent: '#000000',
    success: '#8dffb3',
    failure: '#ffabb7',
    onFailure: '#000000',
    warning: '#fff176',
    warningSurface: '#1c1c00',
    fieldBackground: '#000000',
  },
  'high-contrast-light': {
    pageBackground: '#ffffff',
    surface: '#ffffff',
    border: '#000000',
    text: '#000000',
    subtle: '#000000',
    accent: '#0033cc',
    onAccent: '#ffffff',
    success: '#006400',
    failure: '#a5001e',
    onFailure: '#ffffff',
    warning: '#684300',
    warningSurface: '#fff4c2',
    fieldBackground: '#ffffff',
  },
};

const CSS_TOKEN_NAMES: Readonly<Record<keyof WebThemeTokens, string>> = {
  pageBackground: '--page-background',
  surface: '--surface',
  border: '--border',
  text: '--text',
  subtle: '--subtle',
  accent: '--accent',
  onAccent: '--on-accent',
  success: '--success',
  failure: '--failure',
  onFailure: '--on-failure',
  warning: '--warning',
  warningSurface: '--warning-surface',
  fieldBackground: '--field-background',
};

export interface WebThemeTarget {
  readonly dataset: {[key: string]: string | undefined};
  readonly style: Pick<CSSStyleDeclaration, 'setProperty'>;
}

/** The live-session boundary needed to select a server theme after startup has proved the run. */
export interface WebThemeSession {
  start(): Promise<void>;
  getState(): {readonly hasSnapshot: boolean};
  tuiDefaults(): Promise<Pick<TuiDefaults, 'theme'> | null>;
}

export function webThemeName(value: string | null | undefined): WebThemeName {
  return WEB_THEME_NAMES.includes(value as WebThemeName) ? (value as WebThemeName) : 'dark';
}

/** Apply one complete validated palette to the document. */
export function applyWebTheme(target: WebThemeTarget, value: string | null | undefined): void {
  const name = webThemeName(value);
  target.dataset['theme'] = name;
  const tokens = WEB_THEME_TOKENS[name];
  for (const key of Object.keys(CSS_TOKEN_NAMES) as Array<keyof WebThemeTokens>) {
    target.style.setProperty(CSS_TOKEN_NAMES[key], tokens[key]);
  }
}

/**
 * Apply the default synchronously, then ask a healthy live session for its preference.
 *
 * The optional presentation query must not race the session's stream-first startup. A cold stream
 * failure has made no claim about the independent control channel, and opening that channel just
 * for a theme would turn one observed outage into two banners. Once startup has accepted a fenced
 * snapshot, the query reuses the control channel the session already established.
 */
export async function applySessionTheme(
  target: WebThemeTarget,
  session: WebThemeSession,
): Promise<void> {
  applyWebTheme(target, null);
  try {
    await session.start();
    if (!session.getState().hasSnapshot) return;
    const defaults = await session.tuiDefaults();
    applyWebTheme(target, defaults?.theme);
  } catch {
    applyWebTheme(target, null);
  }
}
