import type {ColorRole} from './themes.js';

/** WCAG 2 minimum contrast for text, and for graphics (focus ring, status dots, chart marks). */
export const TEXT_FLOOR = 4.5;
export const GRAPHIC_FLOOR = 3;

type Channels = readonly [number, number, number];

function parseHex(hex: string): Channels {
  const match = /^#?([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(hex);
  const digits = match?.[1];
  if (digits === undefined) throw new Error(`Invalid hex color: ${hex}`);
  const full = digits.length === 3 ? digits.replace(/./g, digit => digit + digit) : digits;
  const value = Number.parseInt(full.slice(0, 6), 16);
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

function channelLuminance(channel: number): number {
  const ratio = channel / 255;
  return ratio <= 0.03928 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance of `#rgb`, `#rrggbb` or `#rrggbbaa` (alpha is ignored). */
export function relativeLuminance(hex: string): number {
  const [red, green, blue] = parseHex(hex);
  return (
    0.2126 * channelLuminance(red) +
    0.7152 * channelLuminance(green) +
    0.0722 * channelLuminance(blue)
  );
}

/** WCAG contrast ratio between two colors, from 1 (identical) to 21 (black on white). */
export function contrastRatio(left: string, right: string): number {
  const first = relativeLuminance(left);
  const second = relativeLuminance(right);
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

/** `first` mixed with `second`, `share` percent of `first`, channel by channel: CSS `color-mix(in srgb)`. */
export function mixHex(first: string, share: number, second: string): string {
  const [a, b] = [parseHex(first), parseHex(second)];
  const channel = (index: 0 | 1 | 2): string =>
    Math.trunc((a[index] * share + b[index] * (100 - share)) / 100 + 0.5)
      .toString(16)
      .padStart(2, '0');
  return `#${channel(0)}${channel(1)}${channel(2)}`;
}

/**
 * Standard error in the log pane: `err` mixed into `log-text`. The log background is dark in every
 * theme, and a light theme's `err` is drawn for light surfaces, so it is lightened to read there.
 */
export const LOG_ERR_MIX = {role: 'err', share: 60, into: 'log-text', on: 'log-bg'} as const;

/** A chip's fill: its tone mixed into the panel it sits on, and the text of that tone drawn on it. */
export const CHIP_FILL_MIX = {share: 12, into: 'panel'} as const;

export interface ContrastPair {
  readonly foreground: ColorRole;
  readonly background: ColorRole;
  readonly floor: number;
  readonly use: string;
}

function pairs(
  foregrounds: readonly ColorRole[],
  backgrounds: readonly ColorRole[],
  floor: number,
  use: string,
): readonly ContrastPair[] {
  return foregrounds.flatMap(foreground =>
    backgrounds.map(background => ({foreground, background, floor, use})),
  );
}

const SURFACES: readonly ColorRole[] = ['bg', 'panel', 'panel-2'];
const STATUS: readonly ColorRole[] = ['ok', 'warn', 'err'];

/**
 * Every pair every theme must meet. A theme added to `THEMES` is checked against all of them, so
 * the floors are data here and no test names a theme.
 */
export const CONTRAST_PAIRS: readonly ContrastPair[] = [
  ...pairs(['text', 'muted'], SURFACES, TEXT_FLOOR, 'text on a surface'),
  ...pairs([...STATUS, 'accent'], ['panel'], TEXT_FLOOR, 'status or accent as text'),
  ...pairs(['log-text'], ['log-bg'], TEXT_FLOOR, 'log text'),
  ...pairs(['bg'], ['text'], TEXT_FLOOR, 'the primary button, which inverts text and bg'),
  ...pairs(['accent'], SURFACES, GRAPHIC_FLOOR, 'focus ring and selection'),
  ...pairs(['bg'], ['accent'], TEXT_FLOOR, 'the transitional button.primary (bg text on accent)'),
  ...pairs([...STATUS, 'muted'], SURFACES, GRAPHIC_FLOOR, 'status dots'),
  ...pairs([...STATUS, 'accent'], SURFACES, GRAPHIC_FLOOR, 'chart marks'),
];
