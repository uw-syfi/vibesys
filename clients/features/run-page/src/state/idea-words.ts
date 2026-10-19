/**
 * What an idea's status, reason and measure say, written once. The faces that show an idea (the
 * Ideas rows, the Timeline, the open idea's header and body, the hero's selected-idea line) take
 * their words from here, so one fact is never worded two ways. The vocabulary is passed, failed,
 * best, adopted and base (issue #3027): `passed` means the idea beat the input, and a failed idea
 * that was measured says why it failed despite its number.
 */
import type {Candidate, CandidateTree} from '@vibesys/run-state/candidates';
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

/** The figure at the right of an idea's row: its ratio, its failed check's progress, or why not. */
export function figureOf(item: Candidate, inputKnown = true): {figure: string; tone: IdeaTone} {
  if (item.outcome === 'running') return {figure: '', tone: 'muted'};
  const measured = item.measurement;
  const failed = measured?.failed ?? null;
  if (failed !== null) {
    const progress =
      failed.completed !== null && failed.required !== null
        ? `${failed.completed}/${failed.required}${failed.unit === null ? '' : ` ${failed.unit}`}`
        : words(failed.check);
    return {figure: `Failed · ${progress}`, tone: 'err'};
  }
  if (measured?.vsBaseline !== null && measured?.vsBaseline !== undefined) {
    return {figure: ratio(measured.vsBaseline), tone: item.outcome === 'passed' ? 'ok' : 'err'};
  }
  if (item.outcome === 'failed') return {figure: capitalized(outcomeWords(item)), tone: 'err'};
  return inputKnown ? {figure: '–', tone: 'muted'} : {figure: NO_INPUT, tone: 'muted'};
}

/** What an idea measured, against the input; null when it measured nothing. */
export interface Measure {
  /** `1.50×`. */
  readonly ratio: string;
  /** `6,054,154 ops/s`; null when its value is not known. */
  readonly value: string | null;
}

/** The measure as a clause: `1.50× the input (6,054,154 ops/s)`. */
function measureClause(measure: Measure): string {
  return `${measure.ratio} the input${measure.value === null ? '' : ` (${measure.value})`}`;
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
 * The hero's selected-idea words when it has no multiple to show: the status, then why. A failed
 * idea never shows a multiple there (see `resultLine`).
 */
export function selectedWords(item: Candidate, measured: boolean): string {
  if (item.outcome !== 'failed') return statusLabel(item);
  const reason = failureReason(item);
  if (reason !== null) return `Failed · ${reason}`;
  return measured ? 'Failed its checks' : 'Failed · not measured';
}

/**
 * The `vs input` column's cell, from an idea's figure: its multiple when it has one, `no input`
 * when the run has no measurement of its input to compare with, else a dash, the same on every
 * face that has the column (a failed check's progress or reason is not a
 * comparison with the input).
 */
export function vsInputCell(figure: {readonly figure: string; readonly tone: IdeaTone}): {
  readonly figure: string;
  readonly tone: IdeaTone;
} {
  return figure.figure.endsWith('×') || figure.figure === NO_INPUT
    ? figure
    : {figure: '–', tone: 'muted'};
}
