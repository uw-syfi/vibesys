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
  multipleOver,
  type ObjectiveReading,
  readObjectives,
} from './objective-reading.js';
import {type RunningTime, runningTimeOf} from './running-time.js';
import type {RunModel} from './run-model.js';
import {duration, formatValue, ratio} from './span-reading.js';

/** One reading of an objective as the chart draws it. */
export interface ChartReadingView {
  /** The idea it belongs to, which opens; absent for a reading with no idea. */
  readonly id?: string;
  readonly key: string;
  /** What hovering it says: `Keep a free list per core · 8.7 ms · 1.43×`. */
  readonly label: string;
  /** Minutes of running time from the run's start. */
  readonly x: number;
  readonly y: number;
  /** The reading of the idea the reader has open: ringed in the foreground color. */
  readonly selected: boolean;
  /** A step of the line set by the chain the reader points at: ringed in the accent. */
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

/** The share of the largest value left above it, so the top dot is not on the frame. */
const HEADROOM = 1.1;

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

/** A length of running time as the axis words it: to the minute from a minute on (`11h 18m`). */
export function runningWords(seconds: number): string {
  return duration(seconds < 60 ? seconds : Math.round(seconds / 60) * 60);
}

function unitOf(model: RunModel, reading: ObjectiveReading): string {
  if (reading.primary) return objectiveUnit(model.overview);
  const unit = model.candidates.candidates
    .flatMap(item => [
      ...(item.measurement?.readings ?? []),
      ...(item.measurement?.inputReadings ?? []),
    ])
    .find(each => each.name === reading.name && each.unit !== null)?.unit;
  return unit == null ? '' : unit;
}

function readingLabel(
  title: string,
  value: number,
  unit: string,
  multiple: number | null,
): string {
  return [title, `${formatValue(value)}${unit === '' ? '' : ` ${unit}`}`]
    .concat(multiple === null ? [] : [ratio(multiple)])
    .join(' · ');
}

/** What an objective's chart draws once its axes are known. */
interface Frame {
  readonly time: RunningTime;
  /** Run-clock seconds the axis reaches. */
  readonly reached: number;
}

function chartOf(
  model: RunModel,
  reading: ObjectiveReading,
  frame: Frame,
  scrub: number | null,
  focus: string | null,
  lit: ReadonlySet<string>,
): ObjectiveChartView {
  const {time} = frame;
  const minutes = (at: number): number => time.at(at) / 60;
  const unit = unitOf(model, reading);
  const readings = reading.fold.points.map(point => ({
    ...(point.unowned === true ? {} : {id: point.id}),
    key: point.key ?? point.id,
    label: readingLabel(
      point.title,
      point.value,
      unit,
      multipleOver(point.value, reading.input, reading.direction),
    ),
    x: minutes(point.at),
    y: point.value,
    selected: focus !== null && point.id === focus,
    lit: point.best && lit.has(point.id),
  }));
  const end = Math.max(minutes(frame.reached), ...readings.map(point => point.x));
  const steps = reading.fold.points
    .filter(point => point.best)
    .map(point => ({x: minutes(point.at), y: point.value}));
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
    better:
      reading.direction === 'max' ? 'higher' : reading.direction === 'min' ? 'lower' : null,
    unit,
    label: `${name} of each reading over the run's running time, with the best so far`,
    readings,
    steps,
    start: reading.input === null ? null : 'Input',
    baselines,
    xEnd: end,
    xTicks: timeTicks(end * 60),
    yTicks: empty ? [] : valueTicks(low, top),
    yDomain: [low, top > 0 ? top * HEADROOM : 1],
    resumes: time.resumes.map(seconds => seconds / 60),
    caption: `running time · ${runningWords(end * 60)}`,
    cursor: scrub === null ? null : minutes(scrub),
  };
}

/**
 * One chart per objective of `whole`, the run to its live end (never the run at a scrubbed sequence,
 * so scrubbing neither mounts nor unmounts a chart nor rescales its axes). `start` is the run's start
 * on its clock; `now` the live clock (null: the run's latest time bounds the axis, always so when
 * scrubbed); `scrub` the time bar's position in run-clock seconds (null at the live end); `focus` the
 * idea the reader has open and `lit` the ideas on the chain the reader points at.
 */
export function objectiveCharts(
  whole: RunModel,
  start: number,
  now: number | null,
  scrub: number | null = null,
  focus: string | null = null,
  lit: ReadonlySet<string> = NO_IDEAS,
): readonly ObjectiveChartView[] {
  const reached = (scrub === null ? (now ?? whole.latest) : whole.latest) ?? start;
  const frame: Frame = {time: runningTimeOf(whole.sessions ?? [], start), reached};
  return readObjectives(whole).map(reading => chartOf(whole, reading, frame, scrub, focus, lit));
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
