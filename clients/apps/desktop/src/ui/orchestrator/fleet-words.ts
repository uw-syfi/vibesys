/**
 * The words and figures of the fleet's rows. A figure is shown as the host sent it, rounded for the
 * eye; the whole value is the text on hover. Every time is told in Seattle time, the zone named, so
 * a list that spans days never leaves a reader to guess which clock it is on.
 */
import type {FleetObjective, Freshness} from '../../state/index.js';
import {formatFigure} from '../figures.js';

type Tone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

const EXACT = new Intl.NumberFormat('en-US', {maximumFractionDigits: 6});

const figure = formatFigure;

/** A cost in dollars: cents below a hundred, whole dollars above. */
export const dollars = (usd: number): string =>
  usd >= 100
    ? `$${Math.round(usd).toLocaleString('en-US')}`
    : `$${usd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** How long, in its two largest units: `2d 6h`, `5h 20m`, `18m`, `42s`. */
export function span(totalMs: number): string {
  const ms = Math.max(0, totalMs);
  if (ms < MINUTE) return `${Math.floor(ms / SECOND)}s`;
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)}m`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h ${Math.floor((ms % HOUR) / MINUTE)}m`;
  return `${Math.floor(ms / DAY)}d ${Math.floor((ms % DAY) / HOUR)}h`;
}

/** How long ago: `3s ago`. */
export const ago = (elapsedMs: number): string => `${span(elapsedMs)} ago`;

const SEATTLE = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
});

/** A moment in Seattle time with its zone: `Nov 2, 4:10 PM PST`. */
export const seattle = (atMs: number): string => SEATTLE.format(new Date(atMs));

/** How a host's freshness reads, as a word and the tone of its dot. */
export const FRESHNESS_WORD: Readonly<Record<Freshness, {label: string; tone: Tone}>> = {
  live: {label: 'Live', tone: 'ok'},
  'catching-up': {label: 'Catching up', tone: 'live'},
  stale: {label: 'Stale', tone: 'warn'},
  empty: {label: 'Waiting', tone: 'neutral'},
  unavailable: {label: 'Unavailable', tone: 'err'},
};

/** A host stale for less than this has no age worth saying: it has only just lost its link. */
const AGE_WORTH_SAYING_MS = 10 * SECOND;

/**
 * A host's freshness as one phrase: `Stale 14m` once its age is worth saying, `Stale` before that
 * and when it is not known (never `Stale 0s`).
 */
export function freshnessPhrase(freshness: Freshness, staleForMs: number | null): string {
  const {label} = FRESHNESS_WORD[freshness];
  return freshness === 'stale' && staleForMs !== null && staleForMs >= AGE_WORTH_SAYING_MS
    ? `${label} ${span(staleForMs)}`
    : label;
}

/**
 * One objective's result, for the eye: `2.31 tok/s`; with no declared unit, led by the objective's
 * name (`latency 37`) so a bare number is never left to guess; a dash when the head has no value.
 */
export function resultWords(objective: FleetObjective): string {
  if (objective.value === null) return '\u2014';
  return objective.unit === ''
    ? `${objective.name} ${figure(objective.value)}`
    : `${figure(objective.value)} ${objective.unit}`;
}

/** One objective's result, whole, for the pointer: its name, the exact value, and the share of the bound. */
export function resultHint(objective: FleetObjective): string {
  const value =
    objective.value === null
      ? 'no result yet'
      : `${EXACT.format(objective.value)}${objective.unit === '' ? '' : ` ${objective.unit}`}`;
  const share =
    objective.percentOfBound === null
      ? ''
      : `, ${EXACT.format(Math.round(objective.percentOfBound * 10) / 10)}% of the bound`;
  return `${objective.name}: ${value}${share}`;
}

/** A share of a bound as a whole percent: `71%`. */
export const percent = (value: number): string => `${Math.round(value)}%`;
