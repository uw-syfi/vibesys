/**
 * What an idea's status, reason and measure say, written once. The faces that show an idea (the
 * Ideas rows, the Timeline, the open idea's header and body, the hero's selected-idea line) take
 * their words from here, so one fact is never worded two ways. The vocabulary is passed, failed,
 * best, adopted and base (issue #3027): `passed` means the idea beat the input, and a failed idea
 * that was measured says why it failed despite its number.
 */
import type {
  BaseComparison,
  Candidate,
  CandidateTree,
  FailedCheck,
} from '@vibesys/run-state/candidates';
import {againstWords} from './base-words.js';
import {ratio, TURNED_DOWN, words} from './span-reading.js';

/** `text` with its first letter capitalized. */
function capitalized(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

/** `Adopted` or `Best` when `id` is the run's leader, else null. */
export function standingOf(best: CandidateTree['best'], id: string): 'Adopted' | 'Best' | null {
  if (best === null || best.candidate !== id) return null;
  return best.adopted ? 'Adopted' : 'Best';
}

/** A status badge's word: `Passed`, `Failed`, `Running`. The reason is the line's, not the badge's. */
export function statusLabel(item: Candidate): string {
  return capitalized(item.outcome);
}

/**
 * Why a failed idea failed, in words (`implementation failed`), or null when the run gave none.
 * Its own checks turning it down is its result, which `failed` already says, so it is no reason.
 */
function failureReason(item: Candidate): string | null {
  if (item.outcome !== 'failed') return null;
  const why = item.reason ?? item.measurement?.failed?.check ?? null;
  return why === null || why === TURNED_DOWN ? null : words(why);
}

/** An idea's outcome in words: `running`, `passed`, or `failed`, then why when the run says. */
export function outcomeWords(item: Candidate): string {
  const reason = failureReason(item);
  return reason === null ? item.outcome : `failed · ${reason}`;
}

/** What a measured idea's `vs input` cell says when the run has no measurement of its input. */
const NO_INPUT = 'no input';

/** The tone of a figure: a good result, a bad one, or a quiet one. */
export type IdeaTone = 'ok' | 'err' | 'muted';

/** How far a failed check came: `93/100 trials`, or the check's name when it reports no progress. */
function progressOf(failed: FailedCheck): string {
  if (failed.completed === null || failed.required === null) return words(failed.check);
  return `${failed.completed}/${failed.required}${failed.unit === null ? '' : ` ${failed.unit}`}`;
}

/**
 * The figure at the right of an idea's row: its multiple of the input, its failed check's progress,
 * or why not. `vsInput` reads the multiple (`vsInputOf`), the same reading the chart's headline is
 * made of, so a row and a chart never state two multiples of the one input, whether or not the idea's
 * own measurement states a ratio to it; an idea it gives none for keeps the ratio its own
 * measurement states. An idea the framework compared with a base and the run has no ratio to the
 * input for shows the ratio it made, named by that base (`1.10× vs round 2`): `againstWords` words
 * a comparison.
 */
export function figureOf(
  item: Candidate,
  inputKnown = true,
  againstWords: (against: BaseComparison) => string = against =>
    `${ratio(against.ratio)} vs its base`,
  vsInput: (idea: Candidate) => number | null = () => null,
): {figure: string; tone: IdeaTone} {
  if (item.outcome === 'running') return {figure: '', tone: 'muted'};
  const measured = item.measurement;
  const failed = measured?.failed ?? null;
  if (failed !== null) return {figure: `Failed · ${progressOf(failed)}`, tone: 'err'};
  const tone = item.outcome === 'passed' ? 'ok' : 'err';
  // The run's input decides: an idea whose own comparison is not with it (a resumed session's) still
  // has the multiple the chart's tip states. Its own ratio stands only where the run has none.
  const multiple = vsInput(item) ?? measured?.vsBaseline ?? null;
  if (multiple !== null) return {figure: ratio(multiple), tone};
  if (measured?.against != null) {
    return {figure: againstWords(measured.against), tone};
  }
  if (item.outcome === 'failed') return {figure: capitalized(outcomeWords(item)), tone: 'err'};
  return inputKnown ? {figure: '–', tone: 'muted'} : {figure: NO_INPUT, tone: 'muted'};
}

/** A figure as a cell shows it, in a short form that fits the column, and the whole on hover. */
export interface FigureCell {
  readonly figure: string;
  readonly tone: IdeaTone;
  /** The whole cell, when `figure` is its short form. */
  readonly figureTitle?: string;
}

/**
 * The figure of an idea's row in a column that cannot hold a whole base (`1.10× vs round 2`): the
 * short form (`1.10× r2`), with the whole as `figureTitle` when they differ. `cell` is the column's
 * own reading of a figure (`vsInputCell`); the Ideas rows and the Timeline word it once, here.
 */
export function figureCell(
  item: Candidate,
  inputKnown: boolean,
  candidates: readonly Candidate[],
  cell: (figure: {readonly figure: string; readonly tone: IdeaTone}) => {
    readonly figure: string;
    readonly tone: IdeaTone;
  } = figure => figure,
  vsInput?: (idea: Candidate) => number | null,
): FigureCell {
  const whole = cell(
    figureOf(item, inputKnown, against => againstWords(candidates, against, 'whole'), vsInput),
  );
  const short = cell(
    figureOf(item, inputKnown, against => againstWords(candidates, against, 'short'), vsInput),
  );
  return short.figure === whole.figure
    ? whole
    : {figure: short.figure, tone: short.tone, figureTitle: whole.figure};
}

/** What an idea measured, against the input or, with none, its base; null when it measured nothing. */
export interface Measure {
  /** `1.50×`. */
  readonly ratio: string;
  /** `6,054,154 ops/s`; null when its value is not known. */
  readonly value: string | null;
  /** The base the ratio is against (`round 2`); null: the input. */
  readonly base?: string | null;
}

/** The measure as a clause: `1.50× the input (6,054,154 ops/s)`, or `1.10× vs round 2 (…)`. */
function measureClause(measure: Measure): string {
  const against = measure.base == null ? 'the input' : `vs ${measure.base}`;
  return `${measure.ratio} ${against}${measure.value === null ? '' : ` (${measure.value})`}`;
}

/**
 * The open idea's result as one line. A passed idea gives its multiple of the input and, when it
 * leads the run, `Adopted` or `Best` before it, so the pane's badge and body agree. A failed idea
 * gives its reason, or, when it was measured and has none, that it failed its checks despite the
 * number it measured, so a multiple never stands unexplained beside `Failed`.
 */
export function resultLine(
  item: Candidate,
  standing: 'Adopted' | 'Best' | null,
  measure: Measure | null,
): string {
  switch (item.outcome) {
    case 'running':
      return 'Running';
    case 'passed':
      return [standing, standing === null ? 'Passed' : 'passed', measure && measureClause(measure)]
        .filter(part => part !== null)
        .join(' · ');
    case 'failed': {
      const reason = failureReason(item);
      const head =
        reason !== null
          ? capitalized(reason)
          : measure === null
            ? 'Not measured'
            : 'Failed its checks';
      return measure === null ? head : `${head} · measured ${measureClause(measure)}`;
    }
  }
}

/**
 * The `vs input` column's cell, from an idea's figure: its multiple when it has one (over the
 * input, or over its base, named, when the run has no ratio to the input), `no input` when the run
 * has no measurement of its input to compare with, else a dash, the same on every
 * face that has the column (a failed check's progress or reason is not a
 * comparison with the input).
 */
export function vsInputCell(figure: {readonly figure: string; readonly tone: IdeaTone}): {
  readonly figure: string;
  readonly tone: IdeaTone;
} {
  return figure.figure.includes('×') || figure.figure === NO_INPUT
    ? figure
    : {figure: '–', tone: 'muted'};
}
