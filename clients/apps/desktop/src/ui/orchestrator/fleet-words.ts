/**
 * The words and figures of the fleet's rows. A figure is shown as the host sent it, rounded for the
 * eye; the whole value is the text on hover. Every time is told in Seattle time, the zone named, so
 * a list that spans days never leaves a reader to guess which clock it is on.
 */
import type {FleetHostRef, FleetObjective} from '../../state/index.js';
import {span} from '../duration.js';
import {formatFigure} from '../figures.js';
import {hostStatusWord} from '../host-status-words.js';

const EXACT = new Intl.NumberFormat('en-US', {maximumFractionDigits: 6});

const figure = formatFigure;

/** A cost in dollars: cents below a hundred, whole dollars above. */
export const dollars = (usd: number): string =>
  usd >= 100
    ? `$${Math.round(usd).toLocaleString('en-US')}`
    : `$${usd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

export {span};

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

/**
 * What the Runs list says when no host has been read: "Not read yet", or the host's own word when
 * every host that has not been read cannot be (it refused for good), since waiting will not read
 * those. A host that is read but cannot run a run is only being read, and says its cause on its chip.
 */
export function unreadTitle(hosts: readonly Pick<FleetHostRef, 'freshness' | 'status'>[]): string {
  const unread = hosts.filter(h => h.freshness === 'empty' || h.freshness === 'unavailable');
  const [first] = unread;
  return first !== undefined &&
    unread.every(h => h.freshness === 'unavailable' && h.status.kind === 'refused')
    ? hostStatusWord(first.status).label
    : 'Not read yet';
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
