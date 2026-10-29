/**
 * An ended run's journal as a host serves it, for tests: a seeded mix of span events and events the
 * trace never folds, the summary the host would fold from it (`runs.summary`), the page opened on
 * that summary, and a host that answers `runs.read_events` in pages of any size. Names and paths
 * are placeholders.
 */
import type {RunEvent, Span} from '@vibesys/contracts';
import type {HostRun} from '@vibesys/hosts';
import {EMPTY_TRACE, foldTrace, traceSpans} from '@vibesys/run-state/trace';
import {control, RUN, spanEvent, T0} from './live-run-fixtures.js';
import {
  type HistoryReply,
  type RunPageRequest,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from './run-page.js';
import {type SummaryReply, summaryFacts} from './run-summary.js';

/** A run's recorded events, sequences 1 to `through`. */
export interface Journal {
  readonly events: readonly RunEvent[];
  readonly through: number;
}

/** A span that begins at `at`, one in three a planning call. */
function newSpan(count: number, at: number, planning: boolean): Span {
  return {
    id: `flow:s${count}`,
    parent: null,
    type: 'flow',
    name: `step ${count}`,
    status: 'running',
    started_at: at,
    ...(planning ? {label: {kind: 'planning_call'}} : {}),
  } as Span;
}

/** A journal of `length` events: span events (some planning calls) mixed with others. */
export function journalOf(random: {int(n: number): number}, length: number): Journal {
  const running: Span[] = [];
  const events: RunEvent[] = [];
  let count = 0;
  for (let sequence = 1; sequence <= length; sequence += 1) {
    const at = T0 + sequence;
    const choice = random.int(5);
    const picked = running.length === 0 ? -1 : random.int(running.length);
    const span = running[picked];
    if (span !== undefined && choice === 1) {
      const updated = {...span, name: `${span.name}.`};
      running[picked] = updated;
      events.push(spanEvent(sequence, at, 'span_updated', updated));
    } else if (span !== undefined && choice === 2) {
      running.splice(picked, 1);
      const ended = {...span, status: 'done', ended_at: at} as Span;
      events.push(spanEvent(sequence, at, 'span_ended', ended));
    } else if (choice === 0 || (span === undefined && choice < 3)) {
      count += 1;
      const started = newSpan(count, at, random.int(3) === 0);
      running.push(started);
      events.push(spanEvent(sequence, at, 'span_started', started));
    } else {
      events.push(control(sequence, at));
    }
  }
  return {events, through: length};
}

/** The sequences of the journal's events the trace folds, in order. */
export function traceSequences(journal: Journal): number[] {
  return journal.events.flatMap(event =>
    event.type.startsWith('span_') ? [event.sequence ?? 0] : [],
  );
}

const ENDED_RUN: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/project',
  runId: RUN.runId,
  status: 'stopped',
  phase: 'stopped',
  paused: null,
  end: {outcome: 'stopped', reason: 'Stopped by you.', resumable: false},
  startedAt: 1,
  reason: null,
};

const NO_TOKENS = {input: null, cacheRead: null, cacheWrite: null, output: null, reasoning: null};

/** The record of the ended run. */
export const ENDED_RECORD = {kind: 'ended', run: ENDED_RUN} as const;

/** The host's summary of `journal`, as the page takes it. */
export function summaryReplyOf(
  journal: Journal,
): Extract<SummaryReply, {readonly kind: 'summary'}> {
  const facts = summaryFacts(
    {
      throughSequence: journal.through,
      eventCount: journal.events.length,
      journalBytes: 0,
      trace: 'recorded',
      roundsFinished: 0,
      overview: null,
      spans: [...traceSpans(foldTrace(EMPTY_TRACE, journal.events))],
      replies: [],
      tokens: NO_TOKENS,
      usage: [],
      records: [],
      inputGates: [],
      terminal: null,
    },
    RUN.runId,
    'stopped',
  );
  return {kind: 'summary', ...facts};
}

/** The page of the ended run once its summary of `journal` arrived: with the first trace page asked. */
export function openedOn(journal: Journal): {
  readonly state: RunPageState;
  readonly requests: readonly RunPageRequest[];
} {
  const initial = runPageInitial({
    identity: RUN,
    title: {project: '~/project', task: 'serve'},
    hostLabel: 'host-a',
    unavailable: {},
  });
  const asked = runPageStep(initial, {type: 'record', record: ENDED_RECORD});
  const summary = asked.requests.find(request => request.kind === 'read-summary');
  if (summary?.kind !== 'read-summary') throw new Error('no summary asked');
  return runPageStep(asked.state, {
    type: 'summary',
    token: summary.token,
    reply: summaryReplyOf(journal),
    wallMs: 0,
  });
}

/**
 * The host's answer to a read of `max` events from `from`: `size` of them (a host cuts a page at
 * its byte bound), every kind of event in them, ending at the journal's end when they reach it.
 */
export function servedPage(
  journal: Journal,
  from: number,
  max: number,
  size: number,
): Extract<HistoryReply, {readonly kind: 'page'}> {
  const count = Math.max(1, Math.min(max, size));
  const next = Math.min(from + count, journal.through + 1);
  return {
    kind: 'page',
    events: journal.events.filter(
      event => (event.sequence ?? 0) >= from && (event.sequence ?? 0) < next,
    ),
    next,
    complete: next > journal.through,
  };
}

/** The `read-trace` request among `requests`, or null. */
export function traceRequest(requests: readonly RunPageRequest[]) {
  const request = requests.find(each => each.kind === 'read-trace');
  return request?.kind === 'read-trace' ? request : null;
}
