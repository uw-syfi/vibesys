/**
 * How the Cost screen writes numbers and names. Nothing here works out a cost: each function takes
 * a figure the host served and says it in words, the same way for every figure of a screen so
 * that a column of them lines up.
 */
import type {AgentRole, Harness} from '@vibesys/orchestrator-proto';
import type {CostModel, FaultKind} from '../../state/index.js';

/**
 * The words the run's own vocabulary owns and this screen needs. The run screen passes them in, so
 * the Cost feature imports nothing from the feature that shows it.
 */
export interface CostWords {
  /** What a type of agent is called. */
  readonly role: (role: AgentRole) => string;
  /** What a harness is called, for a model that is its default. */
  readonly harness: (harness: Harness) => string;
  /** Why a stream failed, in a sentence a person can act on. */
  readonly fault: (kind: FaultKind) => string;
  /** A moment with its date and the zone it is told in. */
  readonly moment: (atMs: number) => string;
}

/** The mark of a cost that is a floor: turns without a price are behind it. */
export const FLOOR = '≥ ';

/** What a figure the host did not report is written as, never a zero. */
export const NOT_REPORTED = '—';

const COUNT = new Intl.NumberFormat('en-US');

/** A count of turns or agents, with thousands separated: `1,433`. */
export const formatCount = (count: number): string => COUNT.format(count);

/**
 * How many decimals every dollar figure of one screen is written with: cents while the run's cost
 * is small, whole dollars once it is not. One rule for the whole screen, so no figure reads as
 * more exact than the one beside it.
 */
export type Precision = 0 | 2;

export const precisionOf = (totalUsd: number | null): Precision =>
  totalUsd !== null && totalUsd >= 100 ? 0 : 2;

const DOLLARS: Readonly<Record<Precision, Intl.NumberFormat>> = {
  0: new Intl.NumberFormat('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0}),
  2: new Intl.NumberFormat('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}),
};

/**
 * A dollar figure at the screen's precision. A cost above zero that the precision would round to
 * nothing is written `<$1` (or `<$0.01`), never as zero: it is a cost the run has.
 */
export function formatUsd(usd: number, precision: Precision): string {
  const least = precision === 0 ? 1 : 0.01;
  if (usd > 0 && usd < least / 2) return `<$${DOLLARS[precision].format(least)}`;
  return `$${DOLLARS[precision].format(usd)}`;
}

/** A dollar figure the host may not have reported. */
export const usdOrNothing = (usd: number | null, precision: Precision): string | null =>
  usd === null ? null : formatUsd(usd, precision);

/** An amount an hour: `$14.20/h`. */
export const formatRate = (usd: number, precision: Precision): string =>
  `${formatUsd(usd, precision)}/h`;

/** How many decimals a step of this size needs to read exactly (20 is 0, 2.5 is 1, 0.05 is 2). */
function decimalsOf(step: number): number {
  for (let decimals = 0; decimals < 6; decimals += 1) {
    const scaled = step * 10 ** decimals;
    if (Math.abs(scaled - Math.round(scaled)) < 1e-9 * Math.max(1, scaled)) return decimals;
  }
  return 6;
}

/**
 * A value on the chart's axis, written with the decimals the axis's step needs, the same for every
 * figure of the axis (`$0.05 $0.10 $0.15`, `$2.5 $5.0 $7.5`, `$20 $40`): the steps are tidy
 * numbers, so each reads exactly.
 */
export function formatAxisUsd(usd: number, step: number): string {
  const decimals = decimalsOf(step);
  const figure = usd.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `$${figure}`;
}

const UNITS = [
  {suffix: 'k', size: 1e3},
  {suffix: 'M', size: 1e6},
  {suffix: 'B', size: 1e9},
  {suffix: 'T', size: 1e12},
] as const;

/** A count of tokens: whole under a thousand, then one decimal of k, M, B or T (`173.2M`). */
export function formatTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  for (const {suffix, size} of UNITS) {
    const tenths = Math.round((tokens / size) * 10);
    // 999,950 rounds to 1000.0k, which is 1.0M: the next unit says it in fewer figures.
    if (tenths < 10_000 || suffix === 'T') return `${(tenths / 10).toFixed(1)}${suffix}`;
  }
  return String(tokens);
}

export const tokensOrNothing = (tokens: number | null): string | null =>
  tokens === null ? null : formatTokens(tokens);

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** How long into the run a bucket starts: `0`, `20 min`, `1 h 20 min`, `1 d 4 h`. */
export function formatElapsed(elapsedMs: number): string {
  const total = Math.max(0, Math.round(elapsedMs / MINUTE));
  if (total === 0) return '0';
  const days = Math.floor(total / 1440);
  const hours = Math.floor((total % 1440) / 60);
  const minutes = total % 60;
  const parts =
    days > 0
      ? [`${days} d`, ...(hours > 0 ? [`${hours} h`] : [])]
      : [...(hours > 0 ? [`${hours} h`] : []), ...(minutes > 0 ? [`${minutes} min`] : [])];
  return parts.join(' ');
}

/** The steps a chart's labels count in, so a label is a round time into the run. */
const LABEL_STEPS_MS = [
  MINUTE,
  2 * MINUTE,
  5 * MINUTE,
  10 * MINUTE,
  15 * MINUTE,
  30 * MINUTE,
  HOUR,
  2 * HOUR,
  3 * HOUR,
  6 * HOUR,
  12 * HOUR,
  DAY,
  2 * DAY,
  7 * DAY,
  14 * DAY,
  30 * DAY,
] as const;

/** About this many labels under the bars; the chart draws fewer when the width cannot hold them. */
const LABELS = 6;

/**
 * Which buckets to label: every `n`th, where `n` buckets last a round time, so that the labels
 * read `0`, `6 h`, `12 h` and not the times of whichever buckets happened to fit.
 */
export function labelStride(bucketMs: number, buckets: number): number {
  if (bucketMs <= 0 || buckets <= 1) return 1;
  const step =
    LABEL_STEPS_MS.find(each => each >= bucketMs && (buckets * bucketMs) / each <= LABELS) ??
    LABEL_STEPS_MS[LABEL_STEPS_MS.length - 1] ??
    bucketMs;
  return Math.max(1, Math.round(step / bucketMs));
}

/** How long each bar of the spend chart is, in the words of its header: `per hour`, `per 3 hours`, `per day`. */
export function bucketWords(bucketMs: number): string {
  const per = (count: number, unit: string): string =>
    count === 1 ? `per ${unit}` : `per ${formatCount(count)} ${unit}s`;
  const minutes = Math.round(bucketMs / MINUTE);
  if (minutes < 60) return per(minutes, 'minute');
  if (minutes < 24 * 60) return per(Math.round(minutes / 60), 'hour');
  return per(Math.round(minutes / (24 * 60)), 'day');
}

/**
 * The CLI provider and model the finished turns reported. The provider always stays visible: two
 * providers may serve the same model id, and an empty model means that provider's own default.
 */
export function modelName(model: CostModel, words: CostWords): string {
  const harness = words.harness(model.harness);
  return model.model === '' ? `${harness} · default` : `${harness} · ${model.model}`;
}

/** What identifies a model across views of a run. */
export const modelKey = (model: CostModel): string => `${model.harness}/${model.model}`;
