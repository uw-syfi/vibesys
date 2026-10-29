/**
 * The words of a Recent row: when the run began (relative to now), how it came out, and how much
 * it did. Pure functions of a number and a clock; the sidebar's rows compose them.
 */
import type {EndKind} from '@vibesys/hosts';

/** The moment the rows are worded against, and the zone they are worded in (the viewer's own by default). */
export interface Clock {
  readonly now: number;
  readonly timeZone?: string;
}

interface Civil {
  readonly year: number;
  readonly month: number;
  readonly day: number;
}

function civil(at: number, timeZone: string | undefined): Civil {
  const parts = new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    ...(timeZone === undefined ? {} : {timeZone}),
  }).formatToParts(at);
  const part = (type: string): number => Number(parts.find(each => each.type === type)?.value);
  return {year: part('year'), month: part('month'), day: part('day')};
}

function dayNumber(date: Civil): number {
  return Math.round(Date.UTC(date.year, date.month - 1, date.day) / 86_400_000);
}

/** The first moment a run could have begun: the product's first commit (2026-05-07). */
const FIRST_RUN_MS = Date.UTC(2026, 4, 7);

/**
 * When a run began: `07:32` today, `Yesterday 21:10`, `Oct 15` earlier this year, `Oct 15, 2025`
 * before that; null when `at` is no moment a run could have begun (NaN, 0, a near-zero epoch from
 * a clock or field that was never set), which is never worded as a date.
 */
export function formatWhen(at: number, clock: Clock): string | null {
  if (!Number.isFinite(at) || at < FIRST_RUN_MS) return null;
  const {timeZone} = clock;
  const zone = timeZone === undefined ? {} : {timeZone};
  const then = civil(at, timeZone);
  const days = dayNumber(civil(clock.now, timeZone)) - dayNumber(then);
  const time = new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    ...zone,
  }).format(at);
  if (days === 0) return time;
  if (days === 1) return `Yesterday ${time}`;
  const withYear = then.year !== civil(clock.now, timeZone).year;
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    ...(withYear ? {year: 'numeric'} : {}),
    ...zone,
  }).format(at);
}

/** When a run began, always with its day: `Oct 19 04:21`, and the year when it is not this one; null as `formatWhen`. */
export function formatStarted(at: number, clock: Clock): string | null {
  if (!Number.isFinite(at) || at < FIRST_RUN_MS) return null;
  const zone = clock.timeZone === undefined ? {} : {timeZone: clock.timeZone};
  const withYear = civil(at, clock.timeZone).year !== civil(clock.now, clock.timeZone).year;
  const day = new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    ...(withYear ? {year: 'numeric'} : {}),
    ...zone,
  }).format(at);
  const time = new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    ...zone,
  }).format(at);
  return `${day} ${time}`;
}

/** A span in its two largest units: `45s`, `52m`, `1h 05m`. */
export function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/** A count of ideas with its unit. */
export function ideasWord(count: number): string {
  return count === 1 ? '1 idea' : `${count} ideas`;
}

/** A ratio to the input with its unit: `3.83×`. */
export function formatRatio(ratio: number): string {
  return `${ratio.toFixed(2)}×`;
}

/** What a run's row says of how it came out. */
export interface Outcome {
  readonly kind: EndKind;
  /** The look's words, lower case (`finished`, `no winner`, `time limit reached`). */
  readonly word: string;
  /** Whether the run succeeded outright (adopted or kept a result). */
  readonly ok: boolean;
  /** The adopted result as a multiple of the input; null when nothing was adopted or the digest is not read. */
  readonly ratio: number | null;
  /** The ideas it made; null when the digest is not read. */
  readonly ideas: number | null;
}

/**
 * The result of an ended run: its best ratio when it has one and succeeded; for an ending that is
 * not a success the reason instead (`stopped · 2 ideas`, `failed to start`, `no winner`), with the
 * best ratio after it when there is one.
 */
export function resultWords(outcome: Outcome): string {
  const {kind, word, ok, ratio, ideas} = outcome;
  if (kind === 'failed') {
    if (ideas === 0) return 'failed to start';
    return ideas === null ? word : `${word} · ${ideasWord(ideas)}`;
  }
  if (kind === 'stopped') {
    if (ideas === null) return word;
    return ideas === 0 ? `${word} · no ideas` : `${word} · ${ideasWord(ideas)}`;
  }
  if (ok && ratio !== null) return formatRatio(ratio);
  return ratio === null ? word : `${word} · ${formatRatio(ratio)}`;
}
