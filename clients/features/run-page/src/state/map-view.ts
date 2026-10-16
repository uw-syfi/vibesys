/**
 * The Map: the run's spans as a tree, and the selected span's details, as selectors over a trace
 * (the run folded at the page's view, from `traceAt`) and the page's `MapState`. Scrubbing changes
 * only the trace these read, so the Map has no second code path for the past.
 *
 * The tree is the spans' parentage in the order they started, with one presentation of known kinds:
 * a workstream sits under the planning call that started before it under the same parent (the
 * call that planned it), and while every span is shown, a planning call's finished workstreams
 * fold into one group the user opens. The filter keeps the roots as context, the spans in its
 * state, the steps under them, and the steps above them.
 */
import type {Span} from '@vibesys/contracts';
import {type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import type {MapFilter, MapState, RunPageState} from './run-page.js';
import {attrsOf, duration, formatValue, ratio, upperFirst} from './span-reading.js';
import {
  attemptWords,
  bucket,
  ended,
  kindWords,
  openWaits,
  plain,
  shortName,
  statusWords,
  subtitleOf,
  titleOf,
  verdictWords,
  waitPhrase,
} from './span-words.js';
import {type LogStreamItem, logGroups, type Tone} from './views.js';

/** One span in the Map's tree, with the spans shown under it. */
export interface MapNode {
  readonly span: Span;
  readonly children: readonly MapNode[];
}

/** One row of the Map: a span, or a planning call's group of finished workstreams. */
export type MapRow =
  | {
      readonly kind: 'span';
      readonly id: string;
      readonly depth: number;
      readonly title: string;
      readonly subtitle: string;
      readonly state: {readonly tone: Tone; readonly label: string};
      readonly chips: readonly {readonly label: string; readonly tone?: Tone}[];
    }
  | {
      readonly kind: 'group';
      readonly id: string;
      /** The id the group's open state is kept under: its planning call's span id. */
      readonly key: string;
      readonly depth: number;
      readonly title: string;
      readonly subtitle: string;
      readonly open: boolean;
    };

/**
 * The trace as a forest: each span under its parent, in start order, and each workstream under
 * the latest planning call started before it under the same parent. A span whose parent is not
 * in the trace is a root.
 */
export function mapForest(trace: RunTrace): readonly MapNode[] {
  const children = new Map<string, MapNode[]>();
  const roots: MapNode[] = [];
  const planner = new Map<string, string>();
  for (const span of traceSpans(trace)) {
    const own: MapNode[] = [];
    const node: MapNode = {span, children: own};
    children.set(span.id, own);
    const siblings = span.parent === null ? undefined : children.get(span.parent);
    if (span.parent === null || siblings === undefined) {
      roots.push(node);
      continue;
    }
    const kind = span.label?.kind;
    if (kind === 'planning_call') planner.set(span.parent, span.id);
    const call = kind === 'workstream' ? planner.get(span.parent) : undefined;
    (call === undefined ? siblings : (children.get(call) ?? siblings)).push(node);
  }
  return roots;
}

function isWorkstream(node: MapNode): boolean {
  return node.span.label?.kind === 'workstream';
}

function spanRow(node: MapNode, depth: number, walk: Walk): MapRow {
  const {spans} = walk;
  const {span} = node;
  const status = statusWords(span.status);
  const verdict = verdictWords(span);
  const links = (span.label?.links ?? []).map(link => {
    const target = spans.get(link.to);
    return {
      label: `${plain(link.rel).toLowerCase()} ${target === undefined ? link.to : shortName(target)}`,
    };
  });
  const planned = node.children.filter(isWorkstream).length;
  return {
    kind: 'span',
    id: span.id,
    depth,
    title: titleOf(span),
    subtitle: subtitleOf(span, spans, planned, walk.start),
    state: status,
    chips: [...links, verdict ?? status],
  };
}

function groupRow(
  call: MapNode,
  finished: readonly MapNode[],
  depth: number,
  map: MapState,
): Extract<MapRow, {kind: 'group'}> {
  const accepted = finished.filter(node => node.span.label?.verdict === 'accepted').length;
  const other = finished.length - accepted;
  return {
    kind: 'group',
    id: `finished ${call.span.id}`,
    key: call.span.id,
    depth,
    title: `${finished.length} finished workstream${finished.length === 1 ? '' : 's'}`,
    subtitle: `${accepted} accepted · ${other} other`,
    open: map.open.has(call.span.id),
  };
}

interface Walk {
  readonly map: MapState;
  readonly spans: ReadonlyMap<string, Span>;
  /** The run's start on the run clock: its first root's. */
  readonly start: number;
}

function matches(node: MapNode, filter: MapFilter): boolean {
  return filter === 'all' || bucket(node.span.status) === filter;
}

/** The rows under `node`: with every span shown, its finished workstreams in one group. */
function childRows(node: MapNode, depth: number, forced: boolean, walk: Walk): MapRow[] {
  const grouped = walk.map.filter === 'all' && node.span.label?.kind === 'planning_call';
  const finished = grouped
    ? node.children.filter(child => isWorkstream(child) && ended(child.span))
    : [];
  const rest = node.children.filter(child => !finished.includes(child));
  const rows: MapRow[] = [];
  const steps = rest.filter(child => !isWorkstream(child));
  for (const child of steps) rows.push(...nodeRows(child, depth, forced, walk));
  if (finished.length > 0) {
    const group = groupRow(node, finished, depth, walk.map);
    rows.push(group);
    if (group.open) {
      for (const child of finished) rows.push(...nodeRows(child, depth + 1, forced, walk));
    }
  }
  for (const child of rest.filter(isWorkstream)) rows.push(...nodeRows(child, depth, forced, walk));
  return rows;
}

/** `node`'s row and the rows under it, or none when the filter hides them all. */
function nodeRows(node: MapNode, depth: number, forced: boolean, walk: Walk): MapRow[] {
  const root = depth === 0;
  const own = matches(node, walk.map.filter);
  const below = childRows(node, depth + 1, forced || (own && !root), walk);
  if (!(root || forced || own || below.length > 0)) return [];
  return [spanRow(node, depth, walk), ...below];
}

/** The Map's rows, top to bottom, for the trace and the page's filter and open groups. */
export function mapRows(trace: RunTrace, map: MapState): readonly MapRow[] {
  const roots = mapForest(trace);
  const walk: Walk = {map, spans: trace.spans, start: roots[0]?.span.started_at ?? 0};
  return roots.flatMap(root => nodeRows(root, 0, false, walk));
}

/** The selected span: the page's selection while it is in the trace, else the first root. */
export function selectedSpan(trace: RunTrace, map: MapState): Span | null {
  const chosen = map.selected === null ? undefined : trace.spans.get(map.selected);
  return chosen ?? mapForest(trace)[0]?.span ?? null;
}

/** One fact of the detail aside; `to` makes it a link to another span. */
export interface MapFact {
  readonly key: string;
  readonly value: string;
  readonly mono?: boolean;
  readonly to?: string;
}

export interface MapDetail {
  readonly id: string;
  readonly kind: string;
  readonly title: string;
  readonly state: {readonly tone: Tone; readonly label: string};
  readonly facts: readonly MapFact[];
  readonly links: readonly {readonly label: string; readonly to: string}[];
}

function at(seconds: number, start: number): string {
  return `${duration(seconds - start)} into the run`;
}

function timeFacts(span: Span, start: number): MapFact[] {
  const facts: MapFact[] = [{key: 'Started', value: at(span.started_at, start), mono: true}];
  const end = span.ended_at;
  if (end !== null && end !== undefined) {
    facts.push({key: 'Ended', value: at(end, start), mono: true});
    facts.push({key: 'Took', value: duration(end - span.started_at), mono: true});
  }
  return facts;
}

function stateFacts(span: Span, trace: RunTrace, start: number): MapFact[] {
  const facts: MapFact[] = [];
  const verdict = verdictWords(span);
  if (verdict !== null) {
    const reason = span.label?.reason;
    facts.push({
      key: 'Verdict',
      value: reason == null ? verdict.label : `${verdict.label} · ${plain(reason)}`,
    });
  }
  const waiting = openWaits(span);
  if (waiting.length > 0) {
    const words = waiting.map(
      wait => `${upperFirst(waitPhrase(wait, trace.spans))}, since ${at(wait.since, start)}`,
    );
    facts.push({key: 'Waiting on', value: words.join('; ')});
  }
  for (const attempt of span.attempts ?? []) {
    facts.push({key: `Try ${attempt.n} of ${attempt.of}`, value: attemptWords(attempt)});
  }
  if (span.failure !== null && span.failure !== undefined) {
    facts.push({key: 'Failure', value: `${plain(span.failure.kind)}: ${span.failure.message}`});
  }
  if (span.stop !== null && span.stop !== undefined) {
    const landed = span.stop.landed === true ? 'interrupted it' : 'did not interrupt it';
    facts.push({key: 'Stop', value: `Asked ${at(span.stop.requested_at, start)}; ${landed}`});
  }
  return facts;
}

/** A label attribute as the screen writes it: a ratio to the baseline, a number, else as is. */
function attrValue(key: string, value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return key === 'vs_baseline' ? ratio(value) : formatValue(value);
  }
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function attrFacts(span: Span): MapFact[] {
  return Object.entries(attrsOf(span))
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => ({key: plain(key), value: attrValue(key, value), mono: true}));
}

/** Facts with distinct keys: an attribute named like a fact above it keeps its own row. */
function distinct(facts: readonly MapFact[]): MapFact[] {
  const seen = new Map<string, number>();
  return facts.map(fact => {
    const count = (seen.get(fact.key) ?? 0) + 1;
    seen.set(fact.key, count);
    return count === 1 ? fact : {...fact, key: `${fact.key} (${count})`};
  });
}

/** The selected span's details: its state, times, waits, tries, attributes and links. */
export function mapDetail(trace: RunTrace, map: MapState): MapDetail | null {
  const span = selectedSpan(trace, map);
  if (span === null) return null;
  const start = mapForest(trace)[0]?.span.started_at ?? span.started_at;
  const links = (span.label?.links ?? []).map(link => {
    const target = trace.spans.get(link.to);
    return {
      label: `${plain(link.rel)} ${target === undefined ? link.to : shortName(target)}`,
      to: link.to,
    };
  });
  return {
    id: span.id,
    kind: kindWords(span),
    title: titleOf(span),
    state: verdictWords(span) ?? statusWords(span.status),
    facts: distinct([
      {key: 'Status', value: statusWords(span.status).label},
      ...stateFacts(span, trace, start),
      ...timeFacts(span, start),
      ...attrFacts(span),
      {key: 'Span', value: span.id, mono: true},
    ]),
    links,
  };
}

/**
 * The log streams whose owner names `spanId`, labelled as the Logs tab labels them. Only writers
 * that know their span set it, so a step may have none; the Map says so rather than guess.
 */
export function spanLogs(state: RunPageState, spanId: string): readonly LogStreamItem[] {
  const streams =
    state.logs.kind === 'listing' || state.logs.kind === 'listed' ? state.logs.streams : [];
  const owned = new Set(
    streams.filter(stream => stream.owner['span_id'] === spanId).map(stream => stream.id),
  );
  return logGroups(state).flatMap(group => group.streams.filter(stream => owned.has(stream.id)));
}
