/**
 * The design tokens as typed data: the themes, the scales (space, type, size, radius, elevation,
 * motion) and the contrast floors every theme meets. Plain data and pure functions with no DOM,
 * so tests, the generator and the settings feature read them. The generator itself (`css.ts`) is
 * internal to this package: only `scripts/tokens-css.ts` and the tests import it.
 */
export {
  APPEARANCE_MODES,
  type Appearance,
  type AppearanceMode,
  effectiveTheme,
  parseAppearance,
  themesOf,
} from './appearance.js';
export {
  CHIP_FILL_MIX,
  CONTRAST_PAIRS,
  type ContrastPair,
  contrastRatio,
  GRAPHIC_FLOOR,
  LOG_ERR_MIX,
  mixHex,
  relativeLuminance,
  TEXT_FLOOR,
} from './contrast.js';
export {
  type DurationToken,
  ELEVATION,
  type ElevationLevel,
  type ElevationSpec,
  FONT_STACKS,
  type FontFamily,
  type FontWeight,
  MONO_ADVANCE,
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
