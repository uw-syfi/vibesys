/**
 * How the run report writes numbers and times. Times are told in Seattle time, the zone of the
 * people who use the app; the one constant below is the only place that says so.
 */

const DISPLAY_TIME_ZONE = 'America/Los_Angeles';

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const moment = new Intl.DateTimeFormat('en-US', {
  timeZone: DISPLAY_TIME_ZONE,
  weekday: 'short',
  hour: 'numeric',
  minute: '2-digit',
});
const clock = new Intl.DateTimeFormat('en-US', {
  timeZone: DISPLAY_TIME_ZONE,
  hour: 'numeric',
  minute: '2-digit',
});
const fields = new Intl.DateTimeFormat('en-US', {
  timeZone: DISPLAY_TIME_ZONE,
  hourCycle: 'h23',
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: 'numeric',
  minute: 'numeric',
  second: 'numeric',
});

/**
 * The runtimes' date formatters put a narrow no-break space (U+202F) or a plain space before AM or
 * PM, depending on the version of their ICU data. One no-break space (U+00A0) in its place gives the
 * same text in every runtime, and a label that never breaks between the time and AM.
 */
const settled = (text: string): string => text.replace(/[\u202f ]/g, '\u00a0');

/** A day and time of day: `Mon 2:40 AM`. */
export const seattleMoment = (atMs: number): string => {
  const [day = '', ...rest] = moment.format(atMs).split(' ');
  return `${day} ${settled(rest.join(' '))}`;
};

/** A time of day: `2:40 AM`. */
export const seattleClock = (atMs: number): string => settled(clock.format(atMs));

/** How far Seattle's clock is ahead of UTC at an instant (negative: behind), by the zone's rules. */
export function seattleOffsetMs(atMs: number): number {
  const part = new Map(
    fields.formatToParts(Math.floor(atMs / SECOND) * SECOND).map(each => [each.type, each.value]),
  );
  const at = (type: Intl.DateTimeFormatPartTypes): number => Number(part.get(type));
  const shown = Date.UTC(
    at('year'),
    at('month') - 1,
    at('day'),
    at('hour'),
    at('minute'),
    at('second'),
  );
  return shown - Math.floor(atMs / SECOND) * SECOND;
}

/** An axis label for an instant: the time of day for a span under a day and a half, else with its day. */
export const axisMoment = (atMs: number, spanMs: number): string =>
  spanMs < 1.5 * DAY ? seattleClock(atMs) : seattleMoment(atMs);

/**
 * How long ago (`3s ago`, `52m ago`, `5d 19h ago`) and how long (`2d 6h`, `5h 20m`, `18m`, `42s`):
 * the Runs list's own words, so one span reads the same on every screen and a screen never says
 * `5 d ago` where another says `5d 19h`.
 */
export {ago as formatAge, span as formatElapsed} from '../fleet-words.js';

const EXACT = new Intl.NumberFormat('en-US', {maximumSignificantDigits: 6});

/** A number as a figure: three significant digits and a suffix for thousands (`11.1k`, `118`, `0.512`). */
export function formatFigure(value: number): string {
  const size = Math.abs(value);
  const [divisor, suffix] =
    size >= 1e9 ? [1e9, 'G'] : size >= 1e6 ? [1e6, 'M'] : size >= 1e3 ? [1e3, 'k'] : [1, ''];
  const scaled = Number((value / divisor).toPrecision(3));
  return `${scaled === 0 ? 0 : scaled}${suffix}`;
}

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
  return text.length <= limit ? text : `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}
