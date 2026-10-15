/**
 * The design tokens as typed data: the themes, the scales (space, type, size, radius, elevation,
 * motion), the contrast floors every theme meets, and the generator of `tokens.css`. Plain data
 * and pure functions with no DOM, so tests, the generator and the settings feature read them.
 */
export {
  CONTRAST_PAIRS,
  type ContrastPair,
  contrastRatio,
  GRAPHIC_FLOOR,
  relativeLuminance,
  TEXT_FLOOR,
} from './contrast.js';
export {
  LEGACY_ALIASES,
  renderTokensCss,
  TOKENS_CSS_COMMAND,
  TOKENS_CSS_PATH,
  tokensCssDrift,
} from './css.js';
export {
  type DurationToken,
  ELEVATION,
  type ElevationLevel,
  type ElevationSpec,
  FONT_STACKS,
  type FontFamily,
  type FontWeight,
  MOTION,
  type MotionSpec,
  RADIUS,
  type RadiusToken,
  SIZE,
  type SizeToken,
  SPACE,
  type SpaceToken,
  type TextSpec,
  type TextStyle,
  TYPE,
} from './scales.js';
export {
  COLOR_ROLES,
  type ColorRole,
  DEFAULT_THEMES,
  type Hex,
  THEMES,
  type Theme,
  type ThemeMode,
  type ThemeName,
} from './themes.js';
