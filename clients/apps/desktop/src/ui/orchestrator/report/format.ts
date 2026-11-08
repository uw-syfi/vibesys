/**
 * How the run report writes numbers and spans. Its times come from the display clock
 * (`../../display-time.ts`).
 */

/**
 * How long ago (`3s ago`, `52m ago`, `5d 19h ago`) and how long (`2d 6h`, `5h 20m`, `18m`, `42s`):
 * the Runs list's own words, so one span reads the same on every screen and a screen never says
 * `5 d ago` where another says `5d 19h`.
 */
export {ago as formatAge, span as formatElapsed} from '../fleet-words.js';

const EXACT = new Intl.NumberFormat('en-US', {maximumSignificantDigits: 6});

/** A number as a figure (`11.08k`, `118.4`, `0.5123`): the app's one rule, shared with every screen. */
export {formatFigure} from '../../figures.js';

/** A gain as the host oriented it, in two decimals and a times sign (`1.25×`); above 1 is better. */
export const formatGain = (ratio: number): string => `${ratio.toFixed(2)}×`;

/** A number whole, grouped, as a hover writes it (`11,076`). */
export const formatExact = (value: number): string => EXACT.format(value);

/** A figure and its unit: `11.1k tok/s`; the figure alone with no unit. */
export const withUnit = (text: string, unit: string | null): string =>
  unit === null || unit === '' ? text : `${text} ${unit}`;

/** Dollars: `$318`, a tenth of a dollar under a hundred (`$12.4`) and cents under ten (`$4.20`). */
export function formatDollars(usd: number): string {
  if (usd >= 100) return `$${Math.round(usd).toLocaleString('en-US')}`;
  return usd >= 10 ? `$${usd.toFixed(1)}` : `$${usd.toFixed(2)}`;
}

/** At most `limit` characters, the end cut with an ellipsis. */
export function clip(text: string, limit: number): string {
  if (text.length <= limit) return text;
  // Cut on a code-point boundary: `slice` counts UTF-16 units, so it could split an astral
  // character's surrogate pair and leave a lone surrogate (a broken glyph) before the ellipsis.
  const kept = [...text]
    .slice(0, Math.max(0, limit - 1))
    .join('')
    .trimEnd();
  return `${kept}…`;
}
