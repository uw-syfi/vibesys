/** The Web theme's host-default control state. DOM application and persistence stay in the app. */

export {
  WEB_THEME_NAMES,
  WEB_THEME_TOKENS,
  type WebThemeName,
  type WebThemeTokens,
} from './palette.js';
export {
  createHostThemeDefaultStore,
  DEFAULT_WEB_THEME,
  type HostThemeDefaultAction,
  type HostThemeDefaultState,
  hostThemeDefaultMachine,
  hostThemeDefaultView,
  type ResolveHostTheme,
  type WebThemeSession,
} from './theme-default.js';
