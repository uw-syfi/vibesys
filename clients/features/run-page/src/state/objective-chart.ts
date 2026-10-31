/**
 * The performance charts of the run level, one per objective: every reading of the objective as a
 * faint dot, over one line, the best so far, that starts at the input's value and steps at each
 * reading that beat the best before it. The x axis is the run's running time (`running-time.ts`), so
 * the stretches it stood stopped are not on it; a small mark on the axis is each resume.
 *
 * The data is `objective-reading`'s: the line's end, the headline of the chart and an idea's
 * `vs input` are one reading. A pure projection to drawing data and the words on it.
 */
import {baselineReferences} from './baselines.js';
import {metricLabel, objectiveUnit} from './metric-words.js';
import {
  bestSteps,
  multipleOver,
  type ObjectiveReading,
  type ObjectiveSeries,
  objectiveSeriesOf,
  readingKey,
  readObjective,
} from './objective-reading.js';
import type {PerformancePoint} from './performance.js';
import type {RunModel} from './run-model.js';
import {type RunningTime, runningTimeOf, runningWords} from './running-time.js';
import {formatValue, ratio} from './span-reading.js';

/** One reading of an objective as the chart draws it. */
export interface ChartReadingView {
  /** The idea it belongs to, which opens; absent for a reading with no idea. */
  readonly id?: string;
  readonly key: string;
  /** What names it for assistive technology, whole: `Keep a free list per core · 8,700 ms · 1.43×`. */
  readonly label: string;
  /** What hovering it says: its idea (cut to the room there is) and the short figures `8.7 ms · 1.43×`. */
  readonly tip: {readonly lead: string; readonly detail: string};
  /** Minutes of running time from the run's start. */
  readonly x: number;
  readonly y: number;
  /** The reading of the idea the reader has open: ringed in the foreground color. */
  readonly selected: boolean;
  /** A corner of the line set by the chain the reader points at: ringed in the accent. */
  readonly lit: boolean;
}

/** One objective's chart. */
export interface ObjectiveChartView {
  readonly id: string;
  /** The measure in words (`Throughput`). */
  readonly name: string;
  /** Which way is better, for the header; null when the run does not say. */
  readonly better: 'higher' | 'lower' | null;
  /** The unit the axis reads in (`ops/s`); empty when the run names none. */
  readonly unit: string;
  readonly label: string;
  readonly readings: readonly ChartReadingView[];
  /** The best so far: from the input's value at the axis' start, one step per new best, to its end. */
  readonly steps: readonly {readonly x: number; readonly y: number}[];
  /** Words for the line's start, when it starts at the input's value. */
  readonly start: string | null;
  /** One reference line per measured comparison, labelled with its label. */
  readonly baselines: readonly {readonly y: number; readonly label: string}[];
  /** The x axis' end in minutes of running time; it starts at 0. */
  readonly xEnd: number;
  readonly xTicks: readonly {readonly value: number; readonly label: string}[];
  readonly yTicks: readonly {readonly value: number; readonly label: string}[];
  /** The y-axis range: from 0 to the largest value of the whole run, with headroom. */
  readonly yDomain: readonly [number, number];
  /** Where each resume sits on the x axis, in minutes of running time. */
  readonly resumes: readonly number[];
  /** What the axis measures and how much of it the run has: `running time · 25m`. */
  readonly caption: string;
  /** The time bar's position in minutes of running time; null at the live end. */
  readonly cursor: number | null;
}

const NO_IDEAS: ReadonlySet<string> = new Set();

/** The shortest x axis, in minutes: a run that has not begun still has an axis to draw. */
const MIN_AXIS = 1;

/** The share of the largest value left above it, so the top dot is not on the frame. */
const HEADROOM = 1.1;

/** The gridlines of a chart with nothing measured: four, evenly spaced, with no words, so the axes read as a chart. */
const SILENT_TICKS = [0.25, 0.5, 0.75, 1].map(value => ({value, label: ''}));

/** A value on a y axis in the short form its size reads in: `6M`, `12K`, `0.42`. */
const COMPACT = new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 2});

/** Round tick positions over [low, high]: at most a handful, at 1, 2, 2.5 or 5 times a power of ten. */
function valueTicks(low: number, high: number) {
  const span = high - low || Math.abs(high) || 1;
  const raw = span / 4;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * magnitude).find(s => s >= raw) ?? raw;
  const found: {value: number; label: string}[] = [];
  for (let value = Math.ceil(low / step) * step; value <= high + step / 1e6; value += step) {
    const rounded = Number(value.toPrecision(12));
    found.push({value: rounded, label: COMPACT.format(rounded)});
    if (found.length >= 8) break;
  }
  return found;
}

/** The lengths a time tick steps by, in seconds, from a second to a week. */
const TIME_STEPS = [
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800,
  604800,
];

/** The most ticks a time axis carries beside its first. */
const TIME_TICKS = 6;

/** A time on an axis: `0m`, `5m`, `2h`, `1h 30m`; seconds below a minute. */
function timeLabel(seconds: number): string {
  if (seconds < 60) return seconds === 0 ? '0m' : `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const [hours, rest] = [Math.floor(minutes / 60), minutes % 60];
  return rest === 0 ? `${hours}h` : `${hours}h ${String(rest).padStart(2, '0')}m`;
}

/** Ticks over [0, `seconds`] at the shortest round step that leaves at most `TIME_TICKS` of them; values in minutes. */
function timeTicks(seconds: number) {
  const step =
    TIME_STEPS.find(each => seconds / each <= TIME_TICKS) ?? TIME_STEPS[TIME_STEPS.length - 1] ?? 1;
  const found: {value: number; label: string}[] = [];
  for (let at = 0; at <= seconds + step / 1e6; at += step) {
    found.push({value: at / 60, label: timeLabel(at)});
  }
  return found;
}

function unitOf(model: RunModel, reading: ObjectiveReading, alone: boolean): string {
  if (alone) return objectiveUnit(model.overview);
  const unit = model.candidates.candidates
    .flatMap(item => [
      ...(item.measurement?.readings ?? []),
      ...(item.measurement?.inputReadings ?? []),
    ])
    .find(each => each.name === reading.name && each.unit !== null)?.unit;
  return unit == null ? '' : unit;
}

/** A value in the short form a tip says it in: `6.05M`, `385.4`, `8.7`. */
const SHORT = new Intl.NumberFormat('en-US', {notation: 'compact', maximumSignificantDigits: 3});

function shortValue(value: number): string {
  return Math.abs(value) < 1000 ? formatValue(value) : SHORT.format(value);
}

/** The figures of a reading: its value with the unit, then its multiple of the input when it has one. */
function figuresOf(value: string, unit: string, multiple: number | null): string {
  return [`${value}${unit === '' ? '' : ` ${unit}`}`]
    .concat(multiple === null ? [] : [ratio(multiple)])
    .join(' · ');
}

/**
 * The readings the chart draws: one dot for each idea, whatever number of samples the run keeps of
 * it. The samples that set a step of the line stay (the line's corners are dots), and the idea's
 * last sample stands for it when none did; a reading with no idea to open stands for itself.
 */
function onePerIdea(
  points: readonly PerformancePoint[],
  stepKeys: ReadonlySet<string>,
): readonly PerformancePoint[] {
  const owned = points.filter(point => point.unowned !== true);
  const last = new Map(owned.map(point => [point.id, point]));
  const stepped = new Set(owned.filter(point => stepKeys.has(readingKey(point))).map(p => p.id));
  return points.filter(
    point =>
      point.unowned === true ||
      stepKeys.has(readingKey(point)) ||
      (!stepped.has(point.id) && last.get(point.id) === point),
  );
}

/** What a reading says: its whole name for assistive technology, and its short tip. */
function tipOf(
  point: PerformancePoint,
  unit: string,
  multiple: number | null,
): Pick<ChartReadingView, 'label' | 'tip'> {
  return {
    label: [point.title, figuresOf(formatValue(point.value), unit, multiple)].join(' · '),
    tip: {lead: point.title, detail: figuresOf(shortValue(point.value), unit, multiple)},
  };
}

/** What an objective's chart draws once its axes are known. */
interface Frame {
  readonly time: RunningTime;
  /** The run has one objective: its unit is the run's own. */
  readonly alone: boolean;
  /** Run-clock seconds the axis reaches. */
  readonly reached: number;
}

function chartOf(
  model: RunModel,
  reading: ObjectiveReading,
  frame: Frame,
  moment: ChartMoment,
): ObjectiveChartView {
  const {time} = frame;
  const {scrub = null, focus = null, lit = NO_IDEAS} = moment;
  const minutes = (at: number): number => time.at(at) / 60;
  const unit = unitOf(model, reading, frame.alone);
  const stepped = bestSteps(reading);
  const stepKeys = new Set(stepped.flatMap(step => (step.key === null ? [] : [step.key])));
  const readings = onePerIdea(reading.fold.points, stepKeys).map(point => ({
    ...(point.unowned === true ? {} : {id: point.id}),
    key: readingKey(point),
    ...tipOf(point, unit, multipleOver(point.value, reading.input, reading.direction)),
    x: minutes(point.at),
    y: point.value,
    selected: focus !== null && point.id === focus,
    // Only a corner of the line is ringed for the chain: a reading that set no step has no ring.
    lit: lit.has(point.id) && stepKeys.has(readingKey(point)),
  }));
  // What the run has worked, as the time bar says it: the axis may be longer (`MIN_AXIS`), this is not.
  const worked = Math.max(time.at(frame.reached), ...reading.fold.points.map(p => time.at(p.at)));
  const reached = Math.max(MIN_AXIS * 60, worked);
  const end = reached / 60;
  const steps = stepped.map(step => ({x: minutes(step.at), y: step.value}));
  if (reading.input !== null) steps.unshift({x: 0, y: reading.input});
  const last = steps.at(-1);
  if (last !== undefined && last.x < end) steps.push({x: end, y: last.y});
  const baselines = baselineReferences(model.baselines, reading.name).map(each => ({
    y: each.value,
    label: each.label,
  }));
  const ys = [
    0,
    ...readings.map(point => point.y),
    ...(reading.input === null ? [] : [reading.input]),
    ...baselines.map(line => line.y),
  ];
  const [low, top] = [Math.min(...ys), Math.max(...ys)];
  const empty = readings.length === 0 && reading.input === null;
  const name = reading.name === null ? 'Metric' : metricLabel(reading.name);
  return {
    id: reading.name ?? 'metric',
    name,
    better: reading.direction === 'max' ? 'higher' : reading.direction === 'min' ? 'lower' : null,
    unit,
    label: `${name} of each reading over the run's running time, with the best so far`,
    readings,
    steps,
    start: reading.input === null ? null : 'Input',
    baselines,
    xEnd: end,
    xTicks: timeTicks(reached),
    yTicks: empty ? SILENT_TICKS : valueTicks(low, top),
    yDomain: [low, top > 0 ? top * HEADROOM : 1],
    resumes: time.resumes.map(seconds => seconds / 60),
    caption: `running time · ${runningWords(worked)}`,
    cursor: scrub === null ? null : minutes(scrub),
  };
}

/** Where the run is read and what the reader points at, for the charts of one moment. */
export interface ChartMoment {
  /** The run's start on its clock. */
  readonly start: number;
  /** The live clock; null: the run's latest time bounds the axis, always so when scrubbed. */
  readonly now: number | null;
  /** The time bar's position in run-clock seconds; null (the default) at the live end. */
  readonly scrub?: number | null;
  /** The idea the reader has open. */
  readonly focus?: string | null;
  /** The ideas on the chain the reader points at. */
  readonly lit?: ReadonlySet<string>;
}

/**
 * One chart per objective of `whole`, the run to its live end (never the run at a scrubbed sequence,
 * so scrubbing neither mounts nor unmounts a chart nor rescales its axes), as of `moment`.
 * `objectives` are the run's, read from the whole run (`objectiveSeriesOf`).
 */
export function objectiveCharts(
  whole: RunModel,
  moment: ChartMoment,
  objectives: readonly ObjectiveSeries[] = objectiveSeriesOf(whole),
): readonly ObjectiveChartView[] {
  const reached =
    (moment.scrub == null ? (moment.now ?? whole.latest) : whole.latest) ?? moment.start;
  const frame: Frame = {
    time: runningTimeOf(whole.sessions ?? [], moment.start),
    alone: objectives.length < 2,
    reached,
  };
  return objectives.map((series, at) =>
    chartOf(whole, readObjective(whole, series, at === 0, frame.alone), frame, moment),
  );
}

/** What the charts' legend names, in order: the line, the dots, and the ring of the open idea. */
export type ChartLegendEntry = 'best' | 'reading' | 'selected';

/**
 * The legend of `charts`: the best-so-far line and the readings when any chart draws them, and the open
 * idea's ring only while an idea is open and one of the charts holds a reading of it. A run with
 * nothing measured has no legend.
 */
export function chartLegend(charts: readonly ObjectiveChartView[]): readonly ChartLegendEntry[] {
  const drawn = charts.some(chart => chart.readings.length > 0);
  return [
    ...(charts.some(chart => chart.readings.length > 0 || chart.steps.length > 0)
      ? (['best'] as const)
      : []),
    ...(drawn ? (['reading'] as const) : []),
    ...(charts.some(chart => chart.readings.some(point => point.selected))
      ? (['selected'] as const)
      : []),
  ];
}
