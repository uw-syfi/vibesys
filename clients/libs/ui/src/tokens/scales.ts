import type {ColorRole} from './themes.js';

/** Lengths are CSS pixels; durations are milliseconds. */

/** A 4 px scale. */
export type SpaceToken =
  | 'space-0'
  | 'space-1'
  | 'space-2'
  | 'space-3'
  | 'space-4'
  | 'space-5'
  | 'space-6'
  | 'space-7'
  | 'space-8';
export const SPACE: Readonly<Record<SpaceToken, number>> = {
  'space-0': 0,
  'space-1': 2,
  'space-2': 4,
  'space-3': 8,
  'space-4': 12,
  'space-5': 16,
  'space-6': 24,
  'space-7': 32,
  'space-8': 48,
};

/**
 * Control and row heights (`control-sm`, `control`, `row`), the sidebar, page content widths, the
 * columns `Grid` and `Split` wrap at, and the small fixed sizes of the components: the hairline, the
 * focus ring and its offset, icons, the height of a compact wordmark, status dots and the ring of a live one, check boxes, a menu's
 * least width, and the heights of a log pane.
 */
export type SizeToken =
  | 'control-sm'
  | 'control'
  | 'row'
  | 'sidebar'
  | 'rail'
  | 'content'
  | 'content-wide'
  | 'content-narrow'
  | 'column-main'
  | 'column-aside'
  | 'column-field'
  | 'column-tile'
  | 'column-swatch'
  | 'column-figure'
  | 'column-metric'
  | 'column-chart'
  | 'chart-min'
  | 'chart-height'
  | 'pane-list'
  | 'pane-outline'
  | 'pane-master'
  | 'panes-floor'
  | 'row-dense'
  | 'gantt-label'
  | 'gantt-label-max'
  | 'gantt-figure'
  | 'gantt-bar'
  | 'hairline'
  | 'focus-ring'
  | 'focus-offset'
  | 'icon'
  | 'wordmark-compact'
  | 'dot'
  | 'dot-ring'
  | 'check'
  | 'menu'
  | 'log'
  | 'log-sm'
  | 'thread';
export const SIZE: Readonly<Record<SizeToken, number>> = {
  'control-sm': 32,
  control: 40,
  row: 44,
  sidebar: 264,
  rail: 64,
  content: 1060,
  'content-wide': 1100,
  'content-narrow': 900,
  'column-main': 520,
  'column-aside': 300,
  'column-field': 240,
  'column-tile': 200,
  'column-swatch': 160,
  'column-figure': 320,
  'column-metric': 360,
  'column-chart': 248,
  'chart-min': 360,
  'chart-height': 48,
  'pane-list': 380,
  'pane-outline': 340,
  'pane-master': 480,
  'panes-floor': 288,
  'row-dense': 28,
  'gantt-label': 220,
  'gantt-label-max': 420,
  'gantt-figure': 96,
  'gantt-bar': 12,
  hairline: 1,
  'focus-ring': 2,
  'focus-offset': 1,
  icon: 16,
  'wordmark-compact': 40,
  dot: 8,
  'dot-ring': 3,
  check: 18,
  menu: 220,
  log: 320,
  'log-sm': 160,
  thread: 480,
};

/**
 * The longest line of a paragraph, in characters: a paragraph is capped here and wraps, while
 * the column that holds it fills its pane (a cap on a column is dead space beside it).
 */
export const PROSE_MEASURE_CH = 105;

export type RadiusToken = 'radius-sm' | 'radius' | 'radius-lg' | 'radius-full';
export const RADIUS: Readonly<Record<RadiusToken, number>> = {
  'radius-sm': 4,
  radius: 6,
  'radius-lg': 8,
  'radius-full': 9999,
};

export type FontFamily = 'sans' | 'mono';
export const FONT_STACKS: Readonly<Record<FontFamily, string>> = {
  sans: '"Schibsted Grotesk", ui-sans-serif, system-ui, -apple-system, sans-serif',
  mono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
};

/** The weights `fonts.css` ships. */
export type FontWeight = 400 | 500 | 600 | 700;

export type TextStyle =
  | 'display'
  | 'title'
  | 'heading'
  | 'item'
  | 'body'
  | 'body-strong'
  | 'small'
  | 'caption'
  | 'code'
  | 'section'
  | 'metric'
  | 'hero';

export interface TextSpec {
  readonly family: FontFamily;
  readonly size: number;
  readonly line: number;
  readonly weight: FontWeight;
  /** Letter-spacing in em. */
  readonly tracking: number;
  readonly uppercase: boolean;
  /** The role the style is drawn in, where it is not the body's `text`. */
  readonly color: ColorRole | null;
}

function text(
  family: FontFamily,
  size: number,
  line: number,
  weight: FontWeight,
  extra: Partial<Pick<TextSpec, 'tracking' | 'uppercase' | 'color'>> = {},
): TextSpec {
  return {family, size, line, weight, tracking: 0, uppercase: false, color: null, ...extra};
}

export const TYPE: Readonly<Record<TextStyle, TextSpec>> = {
  // The ASCII wordmark only, bold, its rows and columns drawn a hair into each other (a line just
  // under its size, slightly tight tracking) so the block glyphs join into solid letters instead of
  // showing antialiased seams; at most 30 px, sized to its container's width.
  display: text('mono', 30, 29, 700, {tracking: -0.03}),
  title: text('sans', 26, 32, 600, {tracking: -0.01}),
  heading: text('sans', 18, 24, 600),
  // The name of a card, an option or a settings row.
  item: text('sans', 15, 20, 600),
  body: text('sans', 14, 20, 400),
  'body-strong': text('sans', 14, 20, 500),
  small: text('sans', 13, 18, 400),
  caption: text('sans', 12, 16, 500),
  code: text('mono', 12.5, 18, 400),
  section: text('mono', 11.5, 16, 500, {tracking: 0.08, uppercase: true, color: 'muted'}),
  metric: text('mono', 22, 28, 600),
  hero: text('mono', 34, 40, 600, {tracking: -0.02}),
};

/**
 * The advance of a monospace glyph, in em: every glyph of JetBrains Mono (the bundled mono face) is
 * 600 units wide on its 1000-unit em. A label in a mono style is `chars * size * MONO_ADVANCE` wide.
 */
export const MONO_ADVANCE = 0.6;

/** `flat` is everything in the page, `raised` menus and popovers, `overlay` the modal and its scrim. */
export type ElevationLevel = 'flat' | 'raised' | 'overlay';
export interface ElevationSpec {
  readonly shadow: string;
  /** The color drawn behind a modal; null where nothing is dimmed. */
  readonly scrim: string | null;
}
export const ELEVATION: Readonly<Record<ElevationLevel, ElevationSpec>> = {
  flat: {shadow: 'none', scrim: null},
  raised: {shadow: '0 4px 12px rgb(0 0 0 / 0.12)', scrim: null},
  overlay: {shadow: '0 12px 40px rgb(0 0 0 / 0.25)', scrim: 'rgb(10 14 22 / 0.5)'},
};

export type DurationToken = 'instant' | 'fast' | 'base';
export interface MotionSpec {
  readonly duration: Readonly<Record<DurationToken, number>>;
  readonly easing: string;
}
/** Under `prefers-reduced-motion: reduce` the generated CSS sets every duration to 0. */
export const MOTION: MotionSpec = {
  duration: {instant: 0, fast: 120, base: 200},
  easing: 'cubic-bezier(0.2, 0, 0, 1)',
};

/**
 * The categorical palette: the colors a chart or a Gantt gives its series (`series` 0 to 7 is
 * `--series-N`), distinct from one another, from the status colors (ok, warn, err) and from the
 * muted grey. Tuned on the `ink` theme; the same on every theme.
 */
export const SERIES: readonly `#${string}`[] = [
  '#56b4e9',
  '#e69f00',
  '#3dbb8c',
  '#cc79a7',
  '#d9cf4a',
  '#9b82e0',
  '#5ec4c4',
  '#c4a27c',
];
