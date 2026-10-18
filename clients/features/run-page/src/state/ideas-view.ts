/**
 * The run level's Ideas face: one row per candidate, running first and then the rest newest
 * first, the failed shown with the rest unless the filter hides them (`+ N failed` then stands
 * for them). The tree
 * layout (`ideas-tree.ts`) draws the same rows nested under what each built on.
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
  failed: true,
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
  /** `vs best`: the share of the best value (1 is the best); an empty track when not measured. */
  readonly bar: {
    readonly value: number;
    readonly tone: 'ok' | 'muted';
    readonly label: string;
  };
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

/** An idea's outcome in words: `running`, `passed`, or `failed`, then why when it is known. */
export function outcomeWords(item: Candidate): string {
  if (item.outcome !== 'failed') return item.outcome;
  const why = item.reason ?? item.measurement?.failed?.check ?? null;
  return why === null ? 'failed' : `failed · ${words(why)}`;
}

function stateOf(item: Candidate): IdeaRow['state'] {
  const tone = {running: 'live', passed: 'ok', failed: 'err'} as const;
  return {tone: tone[item.outcome], label: outcomeWords(item)};
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
    return {figure: `failed · ${progress}`, tone: 'err'};
  }
  if (measured?.vsBaseline !== null && measured?.vsBaseline !== undefined) {
    return {figure: ratio(measured.vsBaseline), tone: item.outcome === 'passed' ? 'ok' : 'err'};
  }
  if (item.outcome === 'failed') return {figure: outcomeWords(item), tone: 'err'};
  return {figure: '–', tone: 'muted'};
}

/**
 * An idea the orchestrator has not planned yet: it runs, but no agent has said what it tries, so
 * its title is the run's placeholder (`round 5`) and its base is not known.
 */
function isPlanning(item: Candidate): boolean {
  return item.outcome === 'running' && item.idea === null && item.hypothesis === null;
}

/** What a row says before its idea has a name or a base. */
const PLANNING = 'planning…';
const BASE_UNKNOWN = '…';

interface RowContext {
  readonly tree: CandidateTree;
  readonly byId: ReadonlyMap<string, Candidate>;
  readonly path: ReadonlySet<string>;
  readonly best: number | null;
  readonly direction: Direction;
  readonly unit: string;
  readonly changed: ReadonlySet<string>;
}

/** The `vs best` share. Every row holds the column, so an unmeasured one leaves an empty track. */
function barOf(item: Candidate, context: RowContext): IdeaRow['bar'] {
  const value = item.measurement?.metricValue ?? null;
  if (value === null || context.best === null) {
    return {value: 0, tone: 'muted', label: 'not measured'};
  }
  return {
    value: share(value, context.best, context.direction),
    tone: item.outcome === 'passed' ? 'ok' : 'muted',
    label: `${formatValue(value)}${context.unit === '' ? '' : ` ${context.unit}`}`,
  };
}

/** Where the idea started from: unknown while it is planned, else the input or the idea it built on. */
function trailOf(item: Candidate, parent: Candidate | null): IdeaRow['trail'] {
  if (isPlanning(item)) return {label: BASE_UNKNOWN, parent: null};
  return parent === null
    ? {label: 'input', parent: null}
    : {label: parent.title, parent: parent.id};
}

/** An idea's words, none when they only repeat its title (`Try: <title>` included). */
function summaryOf(item: Candidate): string | null {
  const plain = (text: string): string =>
    text
      .trim()
      .replace(/^try:\s*/i, '')
      .replace(/[.\s]+$/, '')
      .toLowerCase();
  return item.idea !== null && plain(item.idea) === plain(item.title) ? null : item.idea;
}

function rowOf(item: Candidate, context: RowContext): IdeaRow {
  const {figure, tone} = figureOf(item);
  const parent = item.parent === null ? null : (context.byId.get(item.parent) ?? null);
  const attempts = item.attempts.length;
  return {
    id: item.id,
    title: isPlanning(item) ? PLANNING : item.title,
    summary: summaryOf(item),
    outcome: item.outcome,
    state: stateOf(item),
    figure,
    figureTone: tone,
    runningSince: item.outcome === 'running' ? item.startedAt : null,
    bar: barOf(item, context),
    badges: [
      ...(context.tree.best?.candidate === item.id
        ? [{label: context.tree.best.adopted ? 'adopted' : 'best', tone: 'ok' as const}]
        : []),
      ...(attempts > 1 ? [{label: `×${attempts}`, tone: 'neutral' as const}] : []),
    ],
    trail: trailOf(item, parent),
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

const rowsMemo = new WeakMap<CandidateTree, ReadonlyMap<string, IdeaRow>>();

/** Every idea's row by id, whatever the filter; the same map while the ideas are unchanged. */
export function ideaRowsById(model: RunModel): ReadonlyMap<string, IdeaRow> {
  const tree = model.candidates;
  const known = rowsMemo.get(tree);
  if (known !== undefined) return known;
  const context: RowContext = {
    tree,
    byId: new Map(tree.candidates.map(item => [item.id, item])),
    path: new Set(tree.bestPath),
    best: bestValue(tree),
    direction: model.direction,
    unit: unitOf(model),
    changed: model.changed,
  };
  const rows = new Map(tree.candidates.map(item => [item.id, rowOf(item, context)]));
  rowsMemo.set(tree, rows);
  return rows;
}

function buildIdeas(model: RunModel, filter: IdeasFilter): IdeasView {
  const tree = model.candidates;
  const counts = {running: 0, passed: 0, failed: 0};
  for (const item of tree.candidates) counts[item.outcome] += 1;
  const rowsById = ideaRowsById(model);
  const query = filter.query.trim();
  const rows: IdeaRow[] = [];
  let hiddenFailed = 0;
  for (const item of ordered(tree.candidates)) {
    if (!matches(item, query)) continue;
    if (!filter[item.outcome]) {
      if (item.outcome === 'failed') hiddenFailed += 1;
      continue;
    }
    const row = rowsById.get(item.id);
    if (row !== undefined) rows.push(row);
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

/** How long ago `since` was on the run clock at `now`, as the rows word it. */
export function ageWords(since: number, now: number | null): string {
  return now === null ? '' : duration(now - since);
}
