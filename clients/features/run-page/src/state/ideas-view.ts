/**
 * The run level's Ideas face and best path: one row per candidate, running first and then the
 * rest newest first, the failed collapsed behind a `+ N failed` row unless the filter shows them,
 * and the winning path as a line of names that folds its middle when long.
 *
 * Rows are memoized on the candidate tree and the filter, so a render with neither changed builds
 * nothing (a tree is the same object until a span changes).
 */
import type {Candidate, CandidateOutcome, CandidateTree} from '@vibesys/run-state/candidates';
import type {Direction, RunModel} from './run-model.js';
import {duration, formatValue, ratio, words} from './span-reading.js';

/** Which ideas the Ideas face shows. */
export interface IdeasFilter {
  readonly query: string;
  readonly passed: boolean;
  readonly running: boolean;
  readonly failed: boolean;
}

export const IDEAS_FILTER_DEFAULT: IdeasFilter = {
  query: '',
  passed: true,
  running: true,
  failed: false,
};

export type IdeaTone = 'ok' | 'err' | 'muted';

/** One idea's row. */
export interface IdeaRow {
  readonly id: string;
  readonly title: string;
  readonly summary: string | null;
  readonly outcome: CandidateOutcome;
  readonly state: {readonly tone: 'live' | 'ok' | 'err'; readonly label: string};
  /** `vs input`: the ratio, the failed check's progress, or (running) the start time. */
  readonly figure: string;
  readonly figureTone: IdeaTone;
  /** Running: when it started on the run clock, so the screen words its age. */
  readonly runningSince: number | null;
  /** `vs best`: the share of the best value (1 is the best); null when nothing was measured. */
  readonly bar: {
    readonly value: number;
    readonly tone: 'ok' | 'muted';
    readonly label: string;
  } | null;
  readonly badges: readonly {readonly label: string; readonly tone: 'ok' | 'neutral'}[];
  /** Where it started from: `input`, else the idea it built on (`parent`, which opens). */
  readonly trail: {readonly label: string; readonly parent: string | null};
  /** On the winning path. */
  readonly stripe: boolean;
  /** Changed or arrived in the events just read (the row flashes once). */
  readonly fresh: boolean;
}

export interface IdeasView {
  readonly rows: readonly IdeaRow[];
  /** Failed ideas the filter hides (the `+ N failed` row); 0 when shown or none. */
  readonly hiddenFailed: number;
  readonly counts: Readonly<Record<CandidateOutcome, number>>;
  readonly total: number;
}

/** The unit the run measures in, for the bar's label; empty when the run named none. */
function unitOf(model: RunModel): string {
  const unit = model.overview?.objective.unit;
  return typeof unit === 'string' ? unit : '';
}

function bestValue(tree: CandidateTree): number | null {
  const id = tree.best?.candidate ?? null;
  const best = tree.candidates.find(item => item.id === id);
  return best?.measurement?.metricValue ?? null;
}

function share(value: number, best: number, direction: Direction): number {
  if (best === 0 || value === 0) return 0;
  return Math.min(1, direction === 'min' ? best / value : value / best);
}

function stateOf(item: Candidate): IdeaRow['state'] {
  if (item.outcome === 'running') return {tone: 'live', label: 'running'};
  if (item.outcome === 'passed') return {tone: 'ok', label: 'passed'};
  const why = item.reason ?? item.measurement?.failed?.check ?? null;
  return {tone: 'err', label: why === null ? 'failed' : `failed · ${words(why)}`};
}

/** The figure at the right of an idea's row: its ratio, its failed check's progress, or why not. */
export function figureOf(item: Candidate): {figure: string; tone: IdeaTone} {
  if (item.outcome === 'running') return {figure: '', tone: 'muted'};
  const measured = item.measurement;
  const failed = measured?.failed ?? null;
  if (failed !== null) {
    const progress =
      failed.completed !== null && failed.required !== null
        ? `${failed.completed}/${failed.required}${failed.unit === null ? '' : ` ${failed.unit}`}`
        : words(failed.check);
    return {figure: progress, tone: 'err'};
  }
  if (measured?.vsBaseline !== null && measured?.vsBaseline !== undefined) {
    return {figure: ratio(measured.vsBaseline), tone: item.outcome === 'passed' ? 'ok' : 'err'};
  }
  return {figure: item.reason === null ? '–' : words(item.reason), tone: 'muted'};
}

interface RowContext {
  readonly tree: CandidateTree;
  readonly byId: ReadonlyMap<string, Candidate>;
  readonly path: ReadonlySet<string>;
  readonly best: number | null;
  readonly direction: Direction;
  readonly unit: string;
  readonly changed: ReadonlySet<string>;
}

function rowOf(item: Candidate, context: RowContext): IdeaRow {
  const {figure, tone} = figureOf(item);
  const value = item.measurement?.metricValue ?? null;
  const parent = item.parent === null ? null : (context.byId.get(item.parent) ?? null);
  const attempts = item.attempts.length;
  return {
    id: item.id,
    title: item.title,
    summary: item.idea,
    outcome: item.outcome,
    state: stateOf(item),
    figure,
    figureTone: tone,
    runningSince: item.outcome === 'running' ? item.startedAt : null,
    bar:
      value === null || context.best === null
        ? null
        : {
            value: share(value, context.best, context.direction),
            tone: item.outcome === 'passed' ? 'ok' : 'muted',
            label: `${formatValue(value)}${context.unit === '' ? '' : ` ${context.unit}`}`,
          },
    badges: [
      ...(context.tree.best?.candidate === item.id
        ? [{label: context.tree.best.adopted ? 'adopted' : 'best', tone: 'ok' as const}]
        : []),
      ...(attempts > 1 ? [{label: `×${attempts}`, tone: 'neutral' as const}] : []),
    ],
    trail:
      parent === null ? {label: 'input', parent: null} : {label: parent.title, parent: parent.id},
    stripe: context.path.has(item.id),
    fresh: context.changed.has(item.id),
  };
}

function matches(item: Candidate, query: string): boolean {
  if (query === '') return true;
  const needle = query.toLowerCase();
  return [item.title, item.idea ?? '', item.hypothesis ?? ''].some(text =>
    text.toLowerCase().includes(needle),
  );
}

/** Running first (newest first), then the rest newest first. */
function ordered(candidates: readonly Candidate[]): Candidate[] {
  const running: Candidate[] = [];
  const rest: Candidate[] = [];
  for (let at = candidates.length - 1; at >= 0; at -= 1) {
    const item = candidates[at] as Candidate;
    (item.outcome === 'running' ? running : rest).push(item);
  }
  return [...running, ...rest];
}

function buildIdeas(model: RunModel, filter: IdeasFilter): IdeasView {
  const tree = model.candidates;
  const counts = {running: 0, passed: 0, failed: 0};
  for (const item of tree.candidates) counts[item.outcome] += 1;
  const context: RowContext = {
    tree,
    byId: new Map(tree.candidates.map(item => [item.id, item])),
    path: new Set(tree.bestPath),
    best: bestValue(tree),
    direction: model.direction,
    unit: unitOf(model),
    changed: model.changed,
  };
  const query = filter.query.trim();
  const rows: IdeaRow[] = [];
  let hiddenFailed = 0;
  for (const item of ordered(tree.candidates)) {
    if (!matches(item, query)) continue;
    if (!filter[item.outcome]) {
      if (item.outcome === 'failed') hiddenFailed += 1;
      continue;
    }
    rows.push(rowOf(item, context));
  }
  return {rows, hiddenFailed, counts, total: tree.candidates.length};
}

/** Counts the ideas views built, for the memoization tests. */
export interface ViewMeter {
  count(units: number): void;
}

const ideasMemo = new WeakMap<CandidateTree, {filter: IdeasFilter; view: IdeasView}>();

/** The Ideas face of `model` under `filter`; the same object while neither changes. */
export function ideasView(model: RunModel, filter: IdeasFilter, meter?: ViewMeter): IdeasView {
  const cached = ideasMemo.get(model.candidates);
  if (cached !== undefined && sameFilter(cached.filter, filter)) return cached.view;
  meter?.count(model.candidates.candidates.length);
  const view = buildIdeas(model, filter);
  ideasMemo.set(model.candidates, {filter, view});
  return view;
}

function sameFilter(a: IdeasFilter, b: IdeasFilter): boolean {
  return (
    a.query === b.query && a.passed === b.passed && a.running === b.running && a.failed === b.failed
  );
}

/** A name on the best path, or the fold standing for the names it hides. */
type PathCrumb =
  | {readonly kind: 'idea'; readonly id: string; readonly label: string}
  | {readonly kind: 'fold'; readonly hidden: number};

export interface BestPathView {
  readonly crumbs: readonly PathCrumb[];
  /** The best's ratio to the input, or null when it measured none. */
  readonly ratio: string | null;
}

/** Shown whole up to this many names; longer paths fold their middle unless opened. */
const PATH_SHOWN = 5;

const pathMemo = new WeakMap<CandidateTree, Map<boolean, BestPathView | null>>();

/**
 * The winning path from the input to the best, folded in the middle when long and not `open`; the
 * same object while the ideas and `open` are unchanged.
 */
export function bestPathView(
  model: RunModel,
  open: boolean,
  meter?: ViewMeter,
): BestPathView | null {
  const tree = model.candidates;
  const views = pathMemo.get(tree) ?? new Map<boolean, BestPathView | null>();
  pathMemo.set(tree, views);
  const cached = views.get(open);
  if (cached !== undefined) return cached;
  meter?.count(tree.candidates.length);
  const view = buildPath(tree, open);
  views.set(open, view);
  return view;
}

function buildPath(tree: CandidateTree, open: boolean): BestPathView | null {
  if (tree.bestPath.length === 0) return null;
  const byId = new Map(tree.candidates.map(item => [item.id, item]));
  const names: PathCrumb[] = tree.bestPath.map(id => ({
    kind: 'idea',
    id,
    label: byId.get(id)?.title ?? id,
  }));
  const crumbs: PathCrumb[] =
    open || names.length <= PATH_SHOWN
      ? names
      : [...names.slice(0, 2), {kind: 'fold', hidden: names.length - 4}, ...names.slice(-2)];
  const best = byId.get(tree.bestPath.at(-1) ?? '');
  const vs = best?.measurement?.vsBaseline ?? null;
  return {crumbs, ratio: vs === null ? null : ratio(vs)};
}

/** How long ago `since` was on the run clock at `now`, as the rows word it. */
export function ageWords(since: number, now: number | null): string {
  return now === null ? '' : duration(now - since);
}
