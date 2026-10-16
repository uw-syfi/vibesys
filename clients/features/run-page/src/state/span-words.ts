/**
 * How one span reads on the Map: its kind's name, title, subtitle, state and facts, in words. The
 * kinds the dynamic search declares (planning call, workstream, agent turn, gate) get their own
 * words; any other kind, or a span with no label, reads plainly from its name, title, status,
 * attributes and links. Nothing is guessed from a span's name.
 */
import type {Span} from '@vibesys/contracts';
import type {MapFilter} from './run-page.js';
import {
  duration,
  formatValue,
  numberAttr,
  ratio,
  stepName,
  stringAttr,
  words,
} from './span-reading.js';
import type {Tone} from './views.js';

type Status = Span['status'];
type Waiting = NonNullable<Span['waiting_on']>[number];

const STATUS: Readonly<Record<Status, {readonly label: string; readonly tone: Tone}>> = {
  pending: {label: 'Not started', tone: 'neutral'},
  running: {label: 'Running', tone: 'live'},
  waiting: {label: 'Waiting', tone: 'warn'},
  done: {label: 'Done', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
  stopped: {label: 'Stopped', tone: 'neutral'},
};

export function statusWords(status: Status): {readonly label: string; readonly tone: Tone} {
  return STATUS[status];
}

const VERDICTS: Readonly<Record<string, {readonly label: string; readonly tone: Tone}>> = {
  accepted: {label: 'Accepted', tone: 'ok'},
  rejected: {label: 'Rejected', tone: 'neutral'},
  passed: {label: 'Passed', tone: 'ok'},
  slower: {label: 'Slower than the input', tone: 'warn'},
  failed_check: {label: 'Failed a check', tone: 'err'},
  adopted: {label: 'Adopted', tone: 'ok'},
  not_adopted: {label: 'Not adopted', tone: 'warn'},
};

/** A word from the wire (`failed_check`) as text (`Failed check`). */
export function plain(word: string): string {
  const spaced = words(word);
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** The span's verdict in words, or null when it has none. */
export function verdictWords(span: Span): {readonly label: string; readonly tone: Tone} | null {
  const verdict = span.label?.verdict;
  if (verdict === null || verdict === undefined) return null;
  return VERDICTS[verdict] ?? {label: plain(verdict), tone: 'neutral'};
}

/** Which filter shows a span: pending counts as waiting, stopped as finished. */
export function bucket(status: Status): Exclude<MapFilter, 'all'> {
  switch (status) {
    case 'running':
      return 'running';
    case 'pending':
    case 'waiting':
      return 'waiting';
    case 'failed':
      return 'failed';
    case 'done':
    case 'stopped':
      return 'finished';
  }
}

/** Whether the span has ended (it will not change state again). */
export function ended(span: Span): boolean {
  return span.status === 'done' || span.status === 'failed' || span.status === 'stopped';
}

const KINDS: Readonly<Record<string, string>> = {
  planning_call: 'Planning call',
  workstream: 'Workstream',
  agent_turn: 'Agent turn',
  gate: 'Gate',
  adoption: 'Adoption',
};

/** What kind of step the span is, in words. */
export function kindWords(span: Span): string {
  if (span.parent === null) return 'Run';
  const kind = span.label?.kind;
  if (kind === undefined) return span.type === 'operation' ? 'Operation' : 'Step';
  return KINDS[kind] ?? plain(kind);
}

/** The span's short name, as a link to it reads: a workstream's hypothesis, else its title. */
export function shortName(span: Span): string {
  if (span.label?.kind === 'workstream') {
    const hypothesis = stringAttr(span, 'hypothesis_id');
    if (hypothesis !== null) return hypothesis;
  }
  return span.label?.title ?? stepName(span.name) ?? span.name;
}

/** How a gate's stages read in its title: a task's build stage is the candidate build. */
const GATE_STAGE_WORDS: Readonly<Record<string, string>> = {build: 'candidate build'};

/** A gate's title (its stages joined by ` + `, as the search labels it) in words. */
function gateTitle(title: string): string {
  return title
    .split(' + ')
    .map(stage => GATE_STAGE_WORDS[stage] ?? stage)
    .join(' + ');
}

/** The span's title on its row. */
export function titleOf(span: Span): string {
  const label = span.label;
  const title = label?.title ?? stepName(span.name) ?? span.name;
  if (span.parent === null) return `Run · ${title}`;
  switch (label?.kind) {
    case 'planning_call':
      return plain(title);
    case 'workstream': {
      const hypothesis = stringAttr(span, 'hypothesis_id');
      return hypothesis === null || hypothesis === title ? title : `${hypothesis} · ${title}`;
    }
    case 'agent_turn': {
      const model = stringAttr(span, 'model');
      return model === null ? title : `${title} · ${model}`;
    }
    case 'gate':
      return `Gate · ${gateTitle(title)}`;
    case 'adoption': {
      const candidate = stringAttr(span, 'candidate');
      return candidate === null ? 'Adopt the input' : `Adopt candidate ${candidate.slice(0, 7)}`;
    }
    default:
      return title;
  }
}

/**
 * What the span waits on that is worth saying. Every live flow also awaits the operator's stop, so
 * the run records a `stop` wait on each from the start; it is something the flow waits on only
 * once a stop was requested.
 */
export function openWaits(span: Span): readonly Waiting[] {
  if (ended(span)) return [];
  const requested = span.stop !== null && span.stop !== undefined;
  return (span.waiting_on ?? []).filter(wait => wait.kind !== 'stop' || requested);
}

/**
 * One source the span waits on, as a phrase to follow "waiting on": an operation or a step reads as
 * its title when it is in the trace. A `slot` is a result another flow fills: a task's (the span
 * `ref` names), all of a group's tasks (`group`), or a signal.
 */
export function waitPhrase(wait: Waiting, spans: ReadonlyMap<string, Span>): string {
  const target = spans.get(wait.ref);
  switch (wait.kind) {
    case 'operation':
      return target === undefined ? 'an operation' : titleOf(target);
    case 'slot':
      if (wait.ref === 'group') return 'its steps to finish';
      return target === undefined ? 'another step’s signal' : `${titleOf(target)} to finish`;
    case 'time':
      return 'a timer';
    case 'stop':
      return 'the stop to land';
  }
}

/** How a try of the span's retry went, in words: `again: build failed`, or `running`. */
export function attemptWords(attempt: NonNullable<Span['attempts']>[number]): string {
  const outcome =
    attempt.outcome === null || attempt.outcome === undefined
      ? 'running'
      : plain(attempt.outcome).toLowerCase();
  const reason =
    attempt.reason === null || attempt.reason === undefined ? '' : `: ${attempt.reason}`;
  return `${outcome}${reason}`;
}

/** A gate's measurement, in words: `41.2 · 1.18× the input`. */
function measured(span: Span): string | null {
  const value = numberAttr(span, 'metric_value');
  if (value === null) return null;
  const vs = numberAttr(span, 'vs_baseline');
  return vs === null ? formatValue(value) : `${formatValue(value)} · ${ratio(vs)} the input`;
}

/** The subtitle a labelled kind gives itself, or null to use the generic one. */
function kindSubtitle(span: Span, children: number): string | null {
  switch (span.label?.kind) {
    case 'planning_call':
      return `${children} workstream${children === 1 ? '' : 's'} planned`;
    case 'gate':
      return measured(span);
    case 'workstream':
      return span.label?.reason == null ? null : plain(span.label.reason);
    default:
      return null;
  }
}

/** How many workstreams the run has started, and how many of them are running. */
function workstreams(spans: ReadonlyMap<string, Span>): string | null {
  const all = [...spans.values()].filter(span => span.label?.kind === 'workstream');
  if (all.length === 0) return null;
  const running = all.filter(span => span.status === 'running').length;
  return `${all.length} workstream${all.length === 1 ? '' : 's'} · ${running} running`;
}

/** When the span ran, relative to the run's start (`start`): how long it took, or when it began. */
function timing(span: Span, start: number): string {
  const end = span.ended_at;
  return end === null || end === undefined
    ? `Started ${duration(span.started_at - start)} into the run`
    : `Took ${duration(end - span.started_at)}`;
}

/**
 * The span's subtitle: what it waits on, its kind's own words, its try, its failure, an unknown
 * kind's name, or when it ran. `start` is the run's start on the run clock.
 */
export function subtitleOf(
  span: Span,
  spans: ReadonlyMap<string, Span>,
  children: number,
  start: number,
): string {
  const waiting = openWaits(span);
  if (waiting.length > 0) {
    return `Waiting on ${waiting.map(wait => waitPhrase(wait, spans)).join(', ')}`;
  }
  const own = span.parent === null ? workstreams(spans) : kindSubtitle(span, children);
  if (own !== null) return own;
  const attempt = (span.attempts ?? []).at(-1);
  if (attempt !== undefined && attempt.of > 1) return `Attempt ${attempt.n} of ${attempt.of}`;
  if (span.failure !== null && span.failure !== undefined) return span.failure.message;
  const kind = span.label?.kind;
  return kind === undefined || kind in KINDS ? timing(span, start) : plain(kind);
}
