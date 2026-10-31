/**
 * The Ideas list of the dashboard as a table: one row per idea with what it built on, its status
 * and cause, what it did to each objective, its multiple of the input, its share of the gain (one
 * objective) and its agent time; the status chips with their counts; the search; the sort by an
 * objective. Pure over the run at the position of the time bar.
 *
 * Every figure of the impact is `idea-impact`'s, which is `objective-reading`'s: the best row's
 * `vs input` is the multiple of the chart's headline.
 */
import type {Candidate} from '@vibesys/run-state/candidates';
import {type BaselineColumns, baselineColumns} from './baselines.js';
import type {Direction} from './direction.js';
import {ideaBases} from './idea-bases.js';
import {
  type Impact,
  impactOf,
  type ObjectiveCell,
  type ObjectiveColumn,
} from './idea-impact.js';
import {
  IDEA_STATUSES,
  type IdeaStatus,
  type IdeaStatusView,
  ideaStatus,
  STATUS_WORDS,
} from './idea-status.js';
import {type IdeasFilter, matchesIdeaQuery} from './ideas-view.js';
import {ideaAncestry} from './ideas-tree.js';
import {metricLabel} from './metric-words.js';
import {unitWords} from './headline.js';
import type {ObjectiveSeries} from './objective-reading.js';
import type {RunModel} from './run-model.js';

/** How many objectives get a column of their own; the rest are counted in the header. */
export const OBJECTIVE_COLUMNS = 4;

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
  /** Its multiple of each comparison baseline, in the table's `baselines` order (a dash for none). */
  readonly baselines: readonly string[];
  /** The gain it added and its share of the whole gain (a whole percent); null without one. */
  readonly share: {readonly gain: number; readonly percent: number} | null;
  /** The time its agents worked on it, in seconds. */
  readonly agentSeconds: number;
  /** An idea built the selected idea on its way: the rows of its lineage carry a bar. */
  readonly lineage: boolean;
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
  readonly sort: IdeasSort;
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

/** The sort of a list nobody sorted: the first objective, best first. */
export const DEFAULT_SORT: IdeasSort = {by: 'objective', index: 0};

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
  model: RunModel,
  objectives: readonly ObjectiveSeries[],
  impact: Impact,
): {readonly columns: readonly TableColumn[]; readonly more: number} {
  const alone = impact.columns.length < 2;
  const all = impact.columns.map((column, index): TableColumn => {
    const title = column.name === null ? 'Metric' : metricLabel(column.name);
    const series = objectives[index] ?? column;
    return {...column, index, title, unit: unitWords(model, series, alone, title).trim()};
  });
  const moved = all.filter(column => column.moved);
  const shown = moved.length > 0 ? moved : all.slice(0, 1);
  return {columns: shown.slice(0, OBJECTIVE_COLUMNS), more: Math.max(0, shown.length - OBJECTIVE_COLUMNS)};
}

/** How a cell of a table with `count` columns says its objective (see v5's "How the impact columns scale"). */
function formOf(count: number): CellForm {
  if (count < 2) return 'value';
  return count === 2 ? 'value-multiple' : 'multiple';
}

/** The change an idea made over every objective it read: `faster` if any improved, else its first change. */
function changeOver(cells: readonly ObjectiveCell[] | undefined) {
  const changes = (cells ?? []).flatMap(cell => (cell.change === null ? [] : [cell.change]));
  if (changes.length === 0) return null;
  return changes.includes('faster') ? 'faster' : (changes[0] ?? null);
}

/** `a` before `b` where `direction` says higher or lower is better; missing values last. */
function byValue(a: number | null, b: number | null, direction: Direction): number {
  if (a === null) return b === null ? 0 : 1;
  if (b === null) return -1;
  return direction === 'min' ? a - b : b - a;
}

/**
 * The Ideas table of `model` under `filter` and `sort`. `objectives` are the run's, read from the
 * whole run so a scrubbed model keeps its columns; `selected` is the idea the charts single out
 * (`ideaAncestry` lights its lineage); `now` closes the turns still open.
 */
export function ideasTable(
  model: RunModel,
  options: {
    readonly objectives?: readonly ObjectiveSeries[] | undefined;
    readonly filter: IdeasFilter;
    /** Null: the default order, running first, then the first objective's best. */
    readonly sort: IdeasSort | null;
    readonly selected: string | null;
    readonly now: number | null;
  },
): IdeasTable {
  const tree = model.candidates;
  const objectives = options.objectives ?? [];
  const impact = impactOf(model, objectives);
  const {columns, more} = columnsOf(model, objectives, impact);
  const bases = ideaBases(tree, model.direction);
  const titles = new Map(tree.candidates.map(item => [item.id, item.title]));
  const lit = ideaAncestry(model, options.selected);
  const statuses = new Map(
    tree.candidates.map(item => [item.id, ideaStatus(item, changeOver(impact.cells.get(item.id)))]),
  );
  const counts = new Map<IdeaStatus, number>();
  for (const {status} of statuses.values()) counts.set(status, (counts.get(status) ?? 0) + 1);
  const chips: StatusChip[] = [
    {status: null, word: 'All', count: tree.candidates.length},
    ...IDEA_STATUSES.filter(status => (counts.get(status) ?? 0) > 0).map(status => ({
      status,
      word: STATUS_WORDS[status],
      count: counts.get(status) ?? 0,
    })),
  ];
  const sole = impact.columns.length === 1;
  const comparisons: BaselineColumns = sole
    ? baselineColumns(model)
    : {labels: [], cells: new Map()};
  const sort = options.sort ?? DEFAULT_SORT;
  const rows = tree.candidates
    .filter(item => matchesIdeaQuery(item, options.filter.query))
    .filter(item => options.filter.status === null || statuses.get(item.id)?.status === options.filter.status)
    .map((item, order) => {
      const all = impact.cells.get(item.id) ?? [];
      const status = statuses.get(item.id) ?? ideaStatus(item, null);
      const base = bases.get(item.id);
      const parent = base?.parent ?? null;
      const gain = all[0]?.gain ?? null;
      const fraction = impact.shares?.get(item.id);
      const row: IdeaTableRow = {
        id: item.id,
        title: item.title,
        adopted: tree.best?.candidate === item.id && tree.best.adopted,
        best: tree.best?.candidate === item.id && !tree.best.adopted,
        builtOn: {
          label:
            parent !== null
              ? (titles.get(parent) ?? '…')
              : base?.kind === 'own work'
                ? 'Own work'
                : 'Input',
          parent,
        },
        status: {...status, word: STATUS_WORDS[status.status]},
        cells: columns.map(column => all[column.index] ?? EMPTY),
        baselines: comparisons.cells.get(item.id) ?? [],
        share:
          sole && gain !== null && fraction !== undefined
            ? {gain, percent: Math.round(fraction * 100)}
            : null,
        agentSeconds: workedSeconds(item, options.now),
        lineage: lit.has(item.id) && item.id !== options.selected,
        fresh: model.changed.has(item.id),
      };
      return {row, order, running: status.status === 'running'};
    });
  const cellOf = (entry: (typeof rows)[number], index: number): number | null =>
    impact.cells.get(entry.row.id)?.[index]?.value ?? null;
  rows.sort((a, b) => {
    const pinned = options.sort === null ? Number(b.running) - Number(a.running) : 0;
    if (pinned !== 0) return pinned;
    const ordered =
      sort.by === 'agent-time'
        ? b.row.agentSeconds - a.row.agentSeconds
        : byValue(
            cellOf(a, sort.index),
            cellOf(b, sort.index),
            impact.columns[sort.index]?.direction ?? model.direction,
          );
    return ordered || b.order - a.order;
  });
  return {
    columns,
    more,
    form: formOf(columns.length),
    sort,
    chips,
    rows: rows.map(entry => entry.row),
    total: tree.candidates.length,
    shares: sole && impact.shares !== null,
    baselines: comparisons.labels,
  };
}

const EMPTY: ObjectiveCell = {value: null, multiple: null, change: null, setBest: false, gain: null};
