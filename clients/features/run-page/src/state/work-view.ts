/**
 * The Work tab as a selector over (the folded run at the view, the tab's filter): every workstream
 * the run started, in start order, with its hypothesis, what it builds on, its agent turns, its
 * gate result and its outcome, filtered by outcome and a search query. A run whose search labels
 * no span lists its flows below the root plainly, by name, title and status.
 */
import type {Span} from '@vibesys/contracts';
import {type RunAtView, spansAtView} from './run-at-view.js';
import {
  descendants,
  duration,
  indexSpans,
  isOpen,
  kindOf,
  latestTime,
  numberAttr,
  ratio,
  type SpanIndex,
  stepName,
  stringAttr,
  titleOf,
  words,
} from './span-reading.js';
import type {Tone} from './views.js';
import type {WorkFilter} from './work-filter.js';

/** Where a row's work stands: its verdict, else its lifecycle. */
export type RowOutcome = 'accepted' | 'rejected' | 'running' | 'done' | 'failed' | 'stopped';

export interface WorkRow {
  readonly key: string;
  /** The hypothesis id, else the span's name. */
  readonly id: string;
  readonly hypothesis: string;
  readonly buildsOn: string;
  readonly turns: string;
  readonly gate: string;
  readonly outcome: {readonly kind: RowOutcome; readonly label: string; readonly tone: Tone};
  readonly took: string;
}

export interface WorkView {
  /** Whether the search labels its spans (rows are its workstreams), or flows are read plainly. */
  readonly labelled: boolean;
  readonly rows: readonly WorkRow[];
  /** How many rows there are before the filter. */
  readonly total: number;
}

const OUTCOME: Readonly<Record<RowOutcome, {label: string; tone: Tone}>> = {
  accepted: {label: 'accepted', tone: 'ok'},
  rejected: {label: 'rejected', tone: 'neutral'},
  running: {label: 'running', tone: 'live'},
  done: {label: 'done', tone: 'ok'},
  failed: {label: 'failed', tone: 'err'},
  stopped: {label: 'stopped', tone: 'neutral'},
};

/** An open row not yet running: shown under "running", labelled for what it is. */
const WAITING = {label: 'waiting', tone: 'warn'} as const;

function rowOutcome(span: Span): RowOutcome {
  const verdict = span.label?.verdict;
  if (verdict === 'accepted' || verdict === 'rejected') return verdict;
  if (isOpen(span)) return 'running';
  return span.status === 'done' || span.status === 'failed' || span.status === 'stopped'
    ? span.status
    : 'running';
}

function hypothesisId(span: Span): string {
  return stringAttr(span, 'hypothesis_id') ?? stepName(span.name) ?? span.name;
}

function buildsOn(index: SpanIndex, span: Span): string {
  const targets = (span.label?.links ?? [])
    .filter(link => link.rel === 'builds_on')
    .map(link => {
      const target = index.byId.get(link.to);
      return target === undefined ? link.to : hypothesisId(target);
    });
  if (targets.length > 0) return targets.join(', ');
  return kindOf(span) === 'workstream' ? 'input' : '–';
}

function turnsOf(below: readonly Span[]): string {
  const turns = below.filter(span => kindOf(span) === 'agent_turn');
  if (turns.length === 0) return '–';
  const roles = new Map<string, number>();
  for (const turn of turns) {
    const role = stringAttr(turn, 'role') ?? titleOf(turn);
    roles.set(role, (roles.get(role) ?? 0) + 1);
  }
  const named = [...roles].map(([role, count]) => (count === 1 ? role : `${role} ×${count}`));
  return `${turns.length} · ${named.join(', ')}`;
}

function gateOf(below: readonly Span[]): string {
  const gate = below.filter(span => kindOf(span) === 'gate').at(-1);
  if (gate === undefined) return '–';
  if (isOpen(gate)) return 'measuring';
  const verdict = gate.label?.verdict;
  const vs = numberAttr(gate, 'vs_baseline');
  const parts = [
    verdict ? words(verdict) : words(gate.status),
    ...(vs === null ? [] : [ratio(vs)]),
  ];
  return parts.join(' · ');
}

function took(span: Span, now: number | null): string {
  const end = span.ended_at ?? (isOpen(span) ? now : null);
  return end === null || end === undefined ? '–' : duration(end - span.started_at);
}

function row(index: SpanIndex, span: Span, now: number | null): WorkRow {
  const below = descendants(index, span);
  const kind = rowOutcome(span);
  return {
    key: span.id,
    id: hypothesisId(span),
    hypothesis: titleOf(span),
    buildsOn: buildsOn(index, span),
    turns: turnsOf(below),
    gate: gateOf(below),
    outcome: {kind, ...(kind === 'running' && span.status !== 'running' ? WAITING : OUTCOME[kind])},
    took: took(span, now),
  };
}

/** Whether `row` is shown under `filter`; the query matches id, hypothesis and builds-on text. */
export function rowMatches(row: WorkRow, filter: WorkFilter): boolean {
  if (filter.outcome !== 'all' && row.outcome.kind !== filter.outcome) return false;
  const query = filter.query.trim().toLowerCase();
  if (query === '') return true;
  return [row.id, row.hypothesis, row.buildsOn].some(text => text.toLowerCase().includes(query));
}

export function workView(run: RunAtView, filter: WorkFilter, now?: number | null): WorkView {
  const spans = spansAtView(run);
  const index = indexSpans(spans);

  const labelled = spans.some(span => kindOf(span) !== null);
  const listed = spans.filter(span =>
    labelled ? kindOf(span) === 'workstream' : span.type === 'flow' && span.parent !== null,
  );
  const clock = now ?? latestTime(spans);
  const rows = listed.map(span => row(index, span, clock));
  return {labelled, rows: rows.filter(item => rowMatches(item, filter)), total: rows.length};
}
