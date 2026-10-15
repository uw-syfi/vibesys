/**
 * The design system: domain-free React components drawn only from the design tokens. Props are data
 * and callbacks; variants are token names; no component takes `className` or `style`, so a screen
 * composed from them needs no CSS of its own. An app imports `./tokens.css`, `./fonts.css` and
 * `./components.css` once.
 */
export {applyTheme, type ThemeTarget} from './apply-theme.js';
export type {
  ColorRole,
  ElevationLevel,
  RadiusToken,
  SizeToken,
  SpaceToken,
  TextStyle,
  ThemeMode,
  ThemeName,
} from './tokens/index.js';
