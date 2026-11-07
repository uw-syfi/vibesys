/**
 * How the run report writes numbers and times. Times are told in Seattle time, the zone of the
 * people who use the app; the one constant below is the only place that says so.
 */

export const DISPLAY_TIME_ZONE = 'America/Los_Angeles';

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

/** How long ago, in the largest whole unit: `12 s ago`, `23 min ago`, `3 h ago`, `2 d ago`. */
export function formatAge(ageMs: number): string {
  const age = Math.max(0, ageMs);
  if (age < MINUTE) return `${Math.floor(age / SECOND)} s ago`;
  if (age < HOUR) return `${Math.floor(age / MINUTE)} min ago`;
  if (age < DAY) return `${Math.floor(age / HOUR)} h ago`;
  return `${Math.floor(age / DAY)} d ago`;
}

/** A span of time in its two largest units: `2d 6h`, `5h 12m`, `42m 10s`, `12s`. */
export function formatElapsed(spanMs: number): string {
  const span = Math.max(0, Math.floor(spanMs / SECOND));
  const days = Math.floor(span / 86_400);
  const hours = Math.floor((span % 86_400) / 3600);
  const minutes = Math.floor((span % 3600) / 60);
  const seconds = span % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

const EXACT = new Intl.NumberFormat('en-US', {maximumSignificantDigits: 6});

/** A number as a figure: three significant digits and a suffix for thousands (`11.1k`, `118`, `0.512`). */
export function formatFigure(value: number): string {
  const size = Math.abs(value);
  const [divisor, suffix] =
    size >= 1e9 ? [1e9, 'G'] : size >= 1e6 ? [1e6, 'M'] : size >= 1e3 ? [1e3, 'k'] : [1, ''];
  const scaled = Number((value / divisor).toPrecision(3));
  return `${scaled === 0 ? 0 : scaled}${suffix}`;
}

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
