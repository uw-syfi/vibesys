/**
 * The Ideas list of the dashboard as a table: one row per idea with what it built on, its status
 * and cause, what it did to each objective, its multiple of the input, its share of the gain (one
 * objective) and its agent time; the status chips with their counts; the search; the sort by an
 * objective. Pure over the run at the position of the time bar.
 *
 * The rows are the ideas of the run as it stood at the position (`model`); every figure of the
 * impact is `idea-impact`'s, read from the whole run cut at the position, which is
 * `objective-reading`'s: the best row's `vs input` is the multiple of the chart's headline.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {
  type BaselineColumns,
  baselineColumns,
  baselineReferences,
  improvement,
  objectiveMetric,
} from './baselines.js';
import type {Direction} from './direction.js';
import {type Change, changeOf, unitWords} from './headline.js';
import {type IdeaBases, ideaBases} from './idea-bases.js';
import {type Impact, impactOf, type ObjectiveCell, type ObjectiveColumn} from './idea-impact.js';
import {
  IDEA_STATUSES,
  type IdeaStatus,
  type IdeaStatusView,
  ideaStatus,
  STATUS_WORDS,
} from './idea-status.js';
import {ideaAncestry} from './ideas-tree.js';
import {type IdeasFilter, matchesIdeaQuery} from './ideas-view.js';
import {metricLabel} from './metric-words.js';
import {type ObjectiveSeries, objectiveSeriesOf} from './objective-reading.js';
import type {RunModel} from './run-model.js';
import type {RunPosition} from './time-position.js';

/** How many objectives get a column of their own; the rest are counted in the header. */
const OBJECTIVE_COLUMNS = 4;

/** What the list is sorted by: an objective (its index among the run's), or the agents' time. */
export type IdeasSort =
  | {readonly by: 'objective'; readonly index: number}
  | {readonly by: 'agent-time'};

/** How a cell says its objective: with the value and the multiple, or the multiple alone. */
export type CellForm = 'value' | 'value-multiple' | 'multiple';

export interface TableColumn extends ObjectiveColumn {
  /** The objective's place among the run's. */
  readonly index: number;
  /** The measure in words: `Throughput`. */
  readonly title: string;
  /** Its unit (`ops/s`); empty for none. */
  readonly unit: string;
}

export interface IdeaTableRow {
  readonly id: string;
  readonly title: string;
  /** The run adopted it. */
  readonly adopted: boolean;
  /** It leads the run, and the run has not adopted it. */
  readonly best: boolean;
  /** What it built on: `input`, its own work, or the idea it started from (`parent` opens it). */
  readonly builtOn: {readonly label: string; readonly parent: string | null};
  readonly status: IdeaStatusView & {readonly word: string};
  /** The idea's cell for each of the table's columns, in their order. */
  readonly cells: readonly ObjectiveCell[];
  /** The idea's reading of each of the run's objectives, by the objective's place; null for none. */
  readonly readings: readonly (number | null)[];
  /** Its multiple of each comparison baseline, in the table's `baselines` order (a dash for none). */
  readonly baselines: readonly string[];
  /** The gain it added and its share of the whole gain (a whole percent); null without one. */
  readonly share: {readonly gain: number; readonly percent: number} | null;
  /** The time its agents worked on it, in seconds. */
  readonly agentSeconds: number;
  /** An idea built the selected idea on its way: the rows of its lineage carry a bar. */
  readonly lineage: boolean;
  /** It is the idea the charts single out. */
  readonly selected: boolean;
  readonly fresh: boolean;
}

export interface StatusChip {
  /** The status the chip shows; null: all. */
  readonly status: IdeaStatus | null;
  readonly word: string;
  readonly count: number;
}

export interface IdeasTable {
  readonly columns: readonly TableColumn[];
  /** Objectives with no column of their own (more than `OBJECTIVE_COLUMNS` that moved). */
  readonly more: number;
  readonly form: CellForm;
  /** The order the rows are in. */
  readonly sort: IdeasSort;
  /** The order of a list nobody sorted: pressing the sort in force restores it. */
  readonly defaultSort: IdeasSort;
  /** Chips with an idea behind them, `All` first. */
  readonly chips: readonly StatusChip[];
  readonly rows: readonly IdeaTableRow[];
  /** Every idea the run holds, whatever the filter. */
  readonly total: number;
  /** Whether the run's one objective has a share of the gain to tell. */
  readonly shares: boolean;
  /**
   * The comparison baselines that get a ratio column each: only a run with one objective has them;
   * with two or more they would multiply the columns, so their ratios are the idea view's.
   */
  readonly baselines: readonly string[];
}

/** The sort of a list nobody sorted: the first objective with a column, best first. */
function defaultSortOf(columns: readonly TableColumn[]): IdeasSort {
  return {by: 'objective', index: columns[0]?.index ?? 0};
}

/** The stretches of `idea`'s agent turns, an open one lasting until `now`. */
function workedSeconds(idea: Candidate, now: number | null): number {
  const stretches: {from: number; to: number}[] = [];
  for (const attempt of idea.attempts) {
    for (const step of attempt.steps) {
      if (step.kind !== 'agent_turn') continue;
      const to = step.endedAt ?? now;
      if (to !== null && step.startedAt < to) stretches.push({from: step.startedAt, to});
    }
  }
  stretches.sort((a, b) => a.from - b.from);
  let total = 0;
  let reached = Number.NEGATIVE_INFINITY;
  for (const each of stretches) {
    const from = Math.max(each.from, reached);
    if (each.to > from) total += each.to - from;
    reached = Math.max(reached, each.to);
  }
  return total;
}

function columnsOf(
  whole: RunModel,
  objectives: readonly ObjectiveSeries[],
  impact: Impact,
): {readonly columns: readonly TableColumn[]; readonly more: number} {
  const alone = impact.columns.length < 2;
  const all = impact.columns.map((column, index): TableColumn => {
    const title = column.name === null ? 'Metric' : metricLabel(column.name);
    const series = objectives[index] ?? column;
    return {...column, index, title, unit: unitWords(whole, series, alone, title).trim()};
  });
  const moved = all.filter(column => column.moved);
  const shown = moved.length > 0 ? moved : all.slice(0, 1);
  return {
    columns: shown.slice(0, OBJECTIVE_COLUMNS),
    more: Math.max(0, shown.length - OBJECTIVE_COLUMNS),
  };
}

/** How a cell of a table with `count` columns says its objective (see v5's "How the impact columns scale"). */
function formOf(count: number): CellForm {
  if (count < 2) return 'value';
  return count === 2 ? 'value-multiple' : 'multiple';
}

/**
 * The change an idea made over every objective it read: `faster` if any improved, else its first
 * change. A run read from events states no series per objective when it has several; its ideas then
 * have the ratio their own measurement states (`vsBaseline`, over the input) for the first.
 */
function changeOver(
  item: Candidate,
  cells: readonly ObjectiveCell[] | undefined,
  model: RunModel,
): Change | null {
  const changes = (cells ?? []).flatMap(cell => (cell.change === null ? [] : [cell.change]));
  if (changes.length > 0) return changes.includes('faster') ? 'faster' : (changes[0] ?? null);
  const own = item.measurement?.vsBaseline ?? item.measurement?.against?.ratio ?? null;
  if (own !== null) return changeOf(improvement(own, model.direction));
  // A run with no reading of its input compares with what it was set against (`vs vLLM`).
  const value = cells?.[0]?.value ?? item.measurement?.metricValue ?? null;
  const reference = baselineReferences(model.baselines, objectiveMetric(model)).find(
    each => each.value > 0,
  );
  if (value === null || reference === undefined) return null;
  return changeOf(improvement(value / reference.value, model.direction));
}

/** Whether `item` has a reading, of an objective or of its own measurement. */
function hasReading(item: Candidate, cells: readonly ObjectiveCell[] | undefined): boolean {
  return item.measurement?.metricValue != null || (cells ?? []).some(cell => cell.value !== null);
}

/** `a` before `b` where `direction` says higher or lower is better; missing values last. */
function byValue(a: number | null, b: number | null, direction: Direction): number {
  if (a === null) return b === null ? 0 : 1;
  if (b === null) return -1;
  return direction === 'min' ? a - b : b - a;
}

/** The chips: every idea, then each status that has one, with its count. */
function chipsOf(
  statuses: ReadonlyMap<string, IdeaStatusView>,
  total: number,
): readonly StatusChip[] {
  const counts = new Map<IdeaStatus, number>();
  for (const {status} of statuses.values()) counts.set(status, (counts.get(status) ?? 0) + 1);
  return [
    {status: null, word: 'All', count: total},
    ...IDEA_STATUSES.filter(status => (counts.get(status) ?? 0) > 0).map(status => ({
      status,
      word: STATUS_WORDS[status],
      count: counts.get(status) ?? 0,
    })),
  ];
}

/** What a row is drawn from, whatever the idea. */
interface RowContext {
  readonly model: RunModel;
  readonly impact: Impact;
  readonly columns: readonly TableColumn[];
  readonly statuses: ReadonlyMap<string, IdeaStatusView>;
  readonly bases: IdeaBases;
  readonly titles: ReadonlyMap<string, string>;
  readonly comparisons: BaselineColumns;
  readonly lit: ReadonlySet<string>;
  readonly selected: string | null;
  readonly now: number | null;
}

/** What an idea built on: the idea it started from, its own work, or the input. */
function builtOnOf(item: Candidate, context: RowContext): IdeaTableRow['builtOn'] {
  const base = context.bases.get(item.id);
  const parent = base?.parent ?? null;
  if (parent !== null) return {label: context.titles.get(parent) ?? '…', parent};
  return {label: base?.kind === 'own work' ? 'Own work' : 'Input', parent: null};
}

function rowOf(item: Candidate, context: RowContext): IdeaTableRow {
  const {model, impact} = context;
  const best = model.candidates.best;
  const all = impact.cells.get(item.id) ?? [];
  const status = context.statuses.get(item.id) ?? ideaStatus(item, null, false);
  const gain = all[0]?.gain ?? null;
  const fraction = impact.shares?.get(item.id);
  return {
    id: item.id,
    title: item.title,
    adopted: best?.candidate === item.id && best.adopted,
    best: best?.candidate === item.id && !best.adopted,
    builtOn: builtOnOf(item, context),
    status: {...status, word: STATUS_WORDS[status.status]},
    cells: context.columns.map(column => all[column.index] ?? EMPTY),
    readings: context.impact.columns.map((_, at) => all[at]?.value ?? null),
    baselines: context.comparisons.cells.get(item.id) ?? [],
    share:
      gain !== null && fraction !== undefined ? {gain, percent: Math.round(fraction * 100)} : null,
    agentSeconds: workedSeconds(item, context.now),
    lineage: context.lit.has(item.id) && item.id !== context.selected,
    selected: item.id === context.selected,
    fresh: model.changed.has(item.id),
  };
}

/**
 * `rows` in the order of `sort`; null is the default: the first objective's best, with the ideas
 * still running first at the run's live end (`pinRunning`; in a replay they sort with the rest, as
 * they were not running then). Ties keep the newest first.
 */
function sorted(
  rows: readonly IdeaTableRow[],
  order: ReadonlyMap<string, number>,
  sort: IdeasSort | null,
  fallback: IdeasSort,
  pinRunning: boolean,
  directions: (index: number) => Direction,
): readonly IdeaTableRow[] {
  const by = sort ?? fallback;
  const running = (row: IdeaTableRow): number => Number(row.status.status === 'running');
  const newest = (a: IdeaTableRow, b: IdeaTableRow): number =>
    (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0);
  const value = (row: IdeaTableRow): number | null =>
    by.by === 'objective' ? (row.readings[by.index] ?? null) : null;
  return [...rows].sort((a, b) => {
    const pinned = sort === null && pinRunning ? running(b) - running(a) : 0;
    if (pinned !== 0) return pinned;
    const ordered =
      by.by === 'agent-time'
        ? b.agentSeconds - a.agentSeconds
        : byValue(value(a), value(b), directions(by.index));
    return ordered || newest(a, b);
  });
}

/**
 * The Ideas table of `model` (the run at the time bar's position) under `filter` and `sort`.
 * `position` is that place on the whole run: the objectives, their readings, best flags and
 * multiples are the whole run's cut there, as the chart's are, so a scrubbed model (which holds
 * none of the writer's decisions) neither loses a column nor a best. `selected` is the idea the
 * charts single out (`ideaAncestry` lights its lineage); `now` closes the turns still open.
 */
export function ideasTable(
  model: RunModel,
  options: {
    readonly position: RunPosition;
    readonly filter: IdeasFilter;
    /** Null: the default order, running first at the live end, then the first objective's best. */
    readonly sort: IdeasSort | null;
    readonly selected: string | null;
    readonly now: number | null;
  },
): IdeasTable {
  const tree = model.candidates;
  const {whole, scrub} = options.position;
  const objectives = objectiveSeriesOf(whole);
  const impact = impactOf(whole, objectives, scrub);
  const {columns, more} = columnsOf(whole, objectives, impact);
  const sole = impact.columns.length === 1;
  const statuses = new Map(
    tree.candidates.map(item => {
      const cells = impact.cells.get(item.id);
      return [item.id, ideaStatus(item, changeOver(item, cells, whole), hasReading(item, cells))];
    }),
  );
  const comparisons = sole ? baselineColumns(model) : {labels: [], cells: new Map()};
  const context: RowContext = {
    model,
    impact,
    columns,
    statuses,
    bases: ideaBases(tree, whole.direction),
    titles: new Map(tree.candidates.map(item => [item.id, item.title])),
    comparisons,
    lit: ideaAncestry(model, options.selected),
    selected: options.selected,
    now: options.now,
  };
  const {query, status} = options.filter;
  const shown = tree.candidates
    .filter(item => matchesIdeaQuery(item, query))
    .filter(item => status === null || statuses.get(item.id)?.status === status);
  const order = new Map(tree.candidates.map((item, at) => [item.id, at]));
  const defaultSort = defaultSortOf(columns);
  return {
    columns,
    more,
    form: formOf(columns.length),
    sort: options.sort ?? defaultSort,
    defaultSort,
    chips: chipsOf(statuses, tree.candidates.length),
    rows: sorted(
      shown.map(item => rowOf(item, context)),
      order,
      options.sort,
      defaultSort,
      scrub === null,
      index => impact.columns[index]?.direction ?? whole.direction,
    ),
    total: tree.candidates.length,
    shares: sole && impact.shares !== null,
    baselines: comparisons.labels,
  };
}

const EMPTY: ObjectiveCell = {
  value: null,
  multiple: null,
  change: null,
  setBest: false,
  gain: null,
};
