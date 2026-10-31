/**
 * The headline of each objective's chart: its best as a multiple of the input in words (`3.83× faster`),
 * the values behind it, and, where there is no multiple to state, why not (`input not measured`,
 * `Unavailable`, `Catching up`). The multiple, the best it is of and the input are `objective-reading`'s,
 * the same reading the chart's line and an idea's `vs input` are made of.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {againstWords} from './base-words.js';
import {
  type BaselineNote,
  type BaselineReference,
  baselineNotes,
  baselineReferences,
  improvement,
} from './baselines.js';
import {metricLabel, objectiveUnit, unitLabel} from './metric-words.js';
import {
  bestSoFar,
  multipleOver,
  type ObjectiveReading,
  type ObjectiveSeries,
  objectiveSeriesOf,
  readObjective,
} from './objective-reading.js';
import {objectivesOf} from './pareto-view.js';
import type {Direction, RunModel} from './run-model.js';
import {formatValue, ratio} from './span-reading.js';

/** One objective of the run: what its chart's header says. */
export interface ObjectiveHeadline {
  readonly id: string;
  /** The measure in words, sentence case: `Total ops/sec`. */
  readonly name: string;
  /** The best's multiple of the input worded (`3.83×`); null before anything beat the input. */
  readonly ratio: string | null;
  /**
   * The figure of the headline: the multiple; else the best's value when the input has none to
   * compare with (`287`); else a dash.
   */
  readonly figure: string;
  /**
   * What the figure means: with a multiple, `faster than input`, `slower than input`, `same as
   * input`; with only a value, its unit; null with neither.
   */
  readonly note: string | null;
  /** The whole headline: `3.83× faster`, `No clear change`, `287 ops/s`, `Unavailable`. */
  readonly words: string;
  readonly tone: 'ok' | 'err' | 'muted';
  /** The best's value and the input's, with the unit: `6,054,154 ops/s · input 1,581,539 ops/s`. */
  readonly detail: string;
  /** The tooltip of the headline: `detail`, or the compared means with their ratio (`518.7 vs 393.4 · 1.32× vs round 9`). */
  readonly hint: string;
}

export interface HeroView {
  /** One headline per objective of the run, in its order. */
  readonly headlines: readonly ObjectiveHeadline[];
  /** The comparisons with no reading, each by its status word, in the task's order. */
  readonly baselineNotes: readonly BaselineNote[];
}

/** Within this of 1 the best reads as the same as the input. */
const SAME = 0.0005;

const NO_CLEAR_CHANGE = 'No clear change';

type Words = Pick<
  ObjectiveHeadline,
  'ratio' | 'figure' | 'note' | 'words' | 'tone' | 'detail' | 'hint'
>;

function bestCandidate(model: RunModel): Candidate | null {
  const id = model.candidates.best?.candidate ?? null;
  return model.candidates.candidates.find(item => item.id === id) ?? null;
}

function verdictOf(better: number): {
  readonly note: string;
  readonly tone: ObjectiveHeadline['tone'];
  readonly words: string;
} {
  if (better > 1 + SAME) {
    return {note: 'faster than input', tone: 'ok', words: `${ratio(better)} faster`};
  }
  if (better < 1 - SAME) {
    return {note: 'slower than input', tone: 'err', words: `${ratio(better)} slower`};
  }
  return {note: 'same as input', tone: 'muted', words: NO_CLEAR_CHANGE};
}

/** The best's reading against one measured comparison. */
interface BaselineRatio {
  readonly label: string;
  readonly better: number;
  /** `1.32×`. */
  readonly ratio: string;
  /** The comparison's value with the unit: `66.2 tok/s`. */
  readonly base: string;
}

/**
 * The best's value as an improvement over each measured comparison of `references` (those with a
 * ratio: a base of zero has none), in their order; empty without a value.
 */
function baselineRatios(
  value: number | null,
  references: readonly BaselineReference[],
  direction: Direction,
): readonly BaselineRatio[] {
  if (value === null) return [];
  return references.flatMap(reference => {
    if (!(reference.value > 0)) return [];
    const better = improvement(value / reference.value, direction);
    const unit = reference.unit === null ? '' : ` ${unitLabel(reference.unit)}`;
    return [
      {
        label: reference.label,
        better,
        ratio: ratio(better),
        base: `${formatValue(reference.value)}${unit}`,
      },
    ];
  });
}

/**
 * The words of an objective with no input to read the best `value` against, set against the run's
 * measured comparisons `references` instead; null without a value or a comparison (`wordsOf` then
 * speaks).
 */
function baselineWords(
  value: number | null,
  unit: string,
  references: readonly BaselineReference[],
  direction: Direction,
): Words | null {
  const [first, ...rest] = baselineRatios(value, references, direction);
  if (value === null || first === undefined) return null;
  const tone = verdictOf(first.better).tone;
  return {
    ratio: first.ratio,
    figure: first.ratio,
    note: `vs ${first.label}`,
    words: `${first.ratio} vs ${first.label}`,
    tone,
    detail: [
      `${formatValue(value)}${unit}`,
      ...rest.map(item => `${item.ratio} vs ${item.label}`),
    ].join(' · '),
    hint: [
      `${formatValue(value)}${unit}`,
      ...[first, ...rest].map(item => `${item.ratio} vs ${item.label} ${item.base}`),
    ].join(' · '),
  };
}

/** Why a measured best has no multiple: the input's measurement failed, or the run has none. */
function noInputWords(model: RunModel): string {
  return model.inputGates.some(failure => failure.supersededBy === null)
    ? 'input not measured'
    : 'no input';
}

/** What an objective with no value says: that the input could not be measured, else that nothing was. */
function notMeasured(model: RunModel): string {
  return model.inputGates.some(failure => failure.supersededBy === null)
    ? 'input not measured'
    : 'nothing measured yet';
}

/** The tooltip of a ratio to the base: the compared means, then the ratio worded by its base. */
function againstHint(
  value: number,
  against: {readonly words: string; readonly baseValue: number} | null,
  otherwise: string,
): string {
  return against === null
    ? otherwise
    : `${formatValue(value)} vs ${formatValue(against.baseValue)} · ${against.words}`;
}

/** What one objective's best read against its input, for the words of its headline. */
interface Reading {
  /** The best's value, null before any reading beat the input. */
  readonly value: number | null;
  readonly input: number | null;
  /** The best's multiple of the input; null when there is none to take. */
  readonly better: number | null;
  /** The unit as a suffix (` ops/s`). */
  readonly unit: string;
  /** Something was measured, though none beat the input. */
  readonly measured: boolean;
  /** The ratio to the idea's base, worded (`1.10× vs round 2`), said in place of why there is no multiple. */
  readonly againstBase: {readonly words: string; readonly baseValue: number} | null;
}

/**
 * The words of an objective whose best read `value` against `input`. With a multiple, the multiple
 * is the headline and the values its detail. Without one the value itself is the headline, its unit
 * the note, and the detail says why there is no multiple: the input's own value if it has one (a
 * zero has no ratio), else that the input was not measured, or has no baseline. With no value the
 * headline says the input stands when something was measured, else that nothing was.
 */
function wordsOf(model: RunModel, read: Reading): Words {
  const {value, input, better, unit} = read;
  const inputPart = input === null ? [] : [`input ${formatValue(input)}${unit}`];
  if (value === null) {
    const standing = read.measured && input !== null;
    const why = standing ? NO_CLEAR_CHANGE : notMeasured(model);
    return {
      ratio: null,
      figure: '–',
      note: null,
      words: why,
      tone: 'muted',
      detail: [why, ...inputPart].join(' · '),
      hint: [why, ...inputPart].join(' · '),
    };
  }
  if (better === null) {
    const shown = `${formatValue(value)}${unit}`;
    return {
      ratio: null,
      figure: formatValue(value),
      note: unit.trim() === '' ? null : unit.trim(),
      words: shown,
      tone: 'muted',
      detail:
        input === null ? (read.againstBase?.words ?? noInputWords(model)) : inputPart.join(''),
      hint:
        input === null
          ? againstHint(value, read.againstBase, noInputWords(model))
          : inputPart.join(''),
    };
  }
  const verdict = verdictOf(better);
  return {
    ratio: ratio(better),
    figure: ratio(better),
    note: verdict.note,
    words: verdict.words,
    tone: verdict.tone,
    detail: [`${formatValue(value)}${unit}`, ...inputPart].join(' · '),
    hint: [`${formatValue(value)}${unit}`, ...inputPart].join(' · '),
  };
}

/** The unit as a suffix (` ops/s`): the run's own, else the name of the measure in lower case. */
function unitWords(model: RunModel, series: ObjectiveSeries, alone: boolean, name: string) {
  if (alone) return ` ${objectiveUnit(model.overview) || name.toLowerCase()}`;
  const unit = model.candidates.candidates
    .flatMap(item => [
      ...(item.measurement?.readings ?? []),
      ...(item.measurement?.inputReadings ?? []),
    ])
    .find(reading => reading.name === series.name && reading.unit !== null)?.unit;
  return ` ${unit == null ? name.toLowerCase() : unitLabel(unit)}`;
}

function headlineOf(
  model: RunModel,
  reading: ObjectiveReading,
  until: number | null,
  catchingUp: boolean,
  alone: boolean,
): ObjectiveHeadline {
  const name = reading.name === null ? 'Metric' : metricLabel(reading.name);
  const id = reading.name ?? 'metric';
  if (catchingUp) {
    const note = model.loadFailure ?? 'Catching up';
    return {
      id,
      name,
      ratio: null,
      figure: '–',
      note,
      words: note,
      tone: 'muted',
      detail: model.loadFailure
        ? 'The run’s history could not be read'
        : 'Reading the run’s committed history',
      hint: '',
    };
  }
  if (reading.fold.refusal) {
    return {
      id,
      name,
      ratio: null,
      figure: '–',
      note: 'Unavailable',
      words: 'Unavailable',
      tone: 'muted',
      detail: reading.fold.refusal,
      hint: '',
    };
  }
  const unit = unitWords(model, reading, alone, name);
  const best = bestSoFar(reading, until);
  const value = best?.value ?? null;
  const better = value === null ? null : multipleOver(value, reading.input, reading.direction);
  const against = reading.primary ? (bestCandidate(model)?.measurement?.against ?? null) : null;
  const read: Reading = {
    value,
    input: reading.input,
    better,
    unit,
    measured: reading.fold.points.some(point => point.result !== 'pending'),
    againstBase:
      against === null
        ? null
        : {
            words: againstWords(model.candidates.candidates, against, 'whole'),
            baseValue: against.baseValue,
          },
  };
  const words =
    (reading.input === null && better === null
      ? baselineWords(
          value,
          unit,
          baselineReferences(model.baselines, reading.name),
          reading.direction,
        )
      : null) ?? wordsOf(model, read);
  return {id, name, ...words};
}

/**
 * The headline of each objective at run-clock time `until` (null: the run's end). `objectives` are
 * the run's, read from the whole run: a scrubbed model of the run's first moments has no overview
 * yet, and the row must not change its charts.
 */
export function heroView(
  model: RunModel,
  objectives: readonly ObjectiveSeries[] = objectivesOf(model),
  until: number | null = null,
): HeroView {
  const catchingUp =
    model.loadFailure !== undefined ||
    (model.progress !== undefined && model.progress.phase !== 'complete');
  const series = objectives.length > 0 ? objectives : objectiveSeriesOf(model);
  return {
    headlines: series.map((each, at) =>
      headlineOf(
        model,
        readObjective(model, each, at === 0, series.length < 2),
        until,
        catchingUp,
        series.length < 2,
      ),
    ),
    baselineNotes: catchingUp ? [] : baselineNotes(model.baselines),
  };
}
