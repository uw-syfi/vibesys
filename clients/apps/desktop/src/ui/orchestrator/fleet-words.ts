/**
 * The words and figures of the fleet's rows. A figure is shown as the host sent it, rounded for the
 * eye; the whole value is the text on hover. A time is told by the display clock with its zone named
 * (`DisplayClock.datedMoment`), so a list that spans days never leaves a reader to guess which
 * clock it is on.
 */
import {AcceleratorVendor, type TaskCard} from '@vibesys/orchestrator-proto';
import type {FleetAttention, FleetObjective} from '../../state/index.js';
import type {DisplayClock} from '../display-time.js';
import {span} from '../duration.js';
import {formatFigure} from '../figures.js';

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

/** `1 job`, `3 jobs`. */
const count = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;

/** Why a run wants a look, as a few words in a tone and the account behind them. */
export function attentionWords(
  attention: FleetAttention,
  nowMs: number,
  clock: DisplayClock,
): {readonly label: string; readonly tone: 'warn' | 'ok'; readonly hint: string} {
  switch (attention.kind) {
    case 'stalled':
      return {
        label: `Stalled ${span(nowMs - attention.sinceMs)}`,
        tone: 'warn',
        hint: `Nothing has moved the run since ${clock.datedMoment(attention.sinceMs)}: no agent is working, no message waits and no job runs`,
      };
    case 'troubles':
      return {
        label: `${count(attention.jobs, 'job', 'jobs')} stuck`,
        tone: 'warn',
        hint: `${count(attention.jobs, 'job is', 'jobs are')} not finished and nothing is working on them`,
      };
    case 'limit':
      return {
        label: `${attention.limit === 'cost' ? 'Cost' : 'Time'} limit reached`,
        tone: 'warn',
        hint: `The run reached its ${attention.limit} limit${attention.atMs === null ? '' : ` at ${clock.datedMoment(attention.atMs)}`}; its lead was told to finish`,
      };
    case 'final':
      return {
        label: 'Final report in',
        tone: 'ok',
        hint: 'The lead posted its final report and has started nothing since',
      };
    case 'kept':
      return {
        label: `${count(attention.checkouts, 'checkout', 'checkouts')} kept`,
        tone: 'warn',
        hint: `The run kept ${count(attention.checkouts, 'working copy', 'working copies')} with changes git does not have; decide what becomes of them`,
      };
  }
}

/** Words of a vendor that lead an authored accelerator name ("AMD MI300X"); a title leaves them out. */
const VENDOR_WORDS: ReadonlySet<string> = new Set([
  'amd',
  'nvidia',
  'aws',
  'intel',
  'google',
  'instinct',
]);

/** The accelerator as a title says it: the authored name without its vendor words ("MI300X"). */
export const acceleratorName = (authored: string): string =>
  authored
    .split(/\s+/)
    .filter(word => word !== '' && !VENDOR_WORDS.has(word.toLowerCase()))
    .join(' ');

/** The hardware half of a title: `1× MI300X`, `CPU`, or empty when the card names none. */
function hardwareWords(card: TaskCard): string {
  const need = card.hardware;
  if (need === undefined) return '';
  if (need.vendor === AcceleratorVendor.CPU) return 'CPU';
  const name = acceleratorName(card.hardwareName);
  return name === '' ? '' : `${need.count}× ${name}`;
}

/**
 * What a run is called: the model or workload and the hardware, `Flint 40B A2.5B · 1× MI300X` or
 * `Tokenizer throughput · CPU`. A card with neither a model nor a workload is led by its heading; a
 * run with no card (recorded before cards) keeps `task`, its folder name, as before. Never a stored
 * field: the title is worded here from the card, and never names a variant.
 */
export function runTitle(card: TaskCard | null, task: string): string {
  const {subject, hardware} = runTitleParts(card, task);
  return [subject, hardware].filter(part => part !== '').join(' · ');
}

/**
 * The two halves of `runTitle`: the model or workload (what gives way when a title is cut short)
 * and the hardware, `1× MI300X` (what tells runs of one model apart, so it is kept whole).
 * `hardware` is empty when the card names none.
 */
function runTitleParts(
  card: TaskCard | null,
  task: string,
): {readonly subject: string; readonly hardware: string} {
  if (card === null) return {subject: task, hardware: ''};
  const subject = [card.modelName, card.workloadName, card.heading, card.task, task].find(
    word => word.trim() !== '',
  );
  return {subject: subject?.trim() ?? task, hardware: hardwareWords(card)};
}
