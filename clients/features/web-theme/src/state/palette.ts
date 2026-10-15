/** Web token names deliberately match the generated server TUI theme contract. */
import type {TuiDefaults} from '@vibesys/contracts';

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

/** One exhaustive palette per generated host theme. This is the runtime source of theme names. */
export const WEB_THEME_TOKENS = {
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
} satisfies Readonly<Record<TuiDefaults['theme'], WebThemeTokens>>;

export type WebThemeName = keyof typeof WEB_THEME_TOKENS;

/** Runtime picker order derived from the exhaustive palette rather than maintained separately. */
export const WEB_THEME_NAMES = Object.freeze(Object.keys(WEB_THEME_TOKENS) as WebThemeName[]);
