/**
 * A live run on a remote host as the page meets it, for tests: the page opened on its record (which
 * asks for the run's summary and attaches), the events a run emits, and the host's own clock
 * (`T0`, epoch seconds). Names and paths are placeholders.
 */
import type {RunEvent, Span} from '@vibesys/contracts';
import type {HostRun} from '@vibesys/hosts';
import {
  type RunPageAction,
  type RunPageRequest,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from './run-page.js';

export const RUN = {host: 'ssh:host-a', runId: '20261017-010000'} as const;

/** 2026-10-17T01:00:00Z, where the run's host clock stood when the run began. */
export const T0 = Date.UTC(2026, 9, 17, 1, 0, 0) / 1000;
export const MINUTE = 60;

const HOST_RUN: HostRun = {
  id: 'a1b2c3d4e5f6',
  projectRoot: '~/project',
  runId: RUN.runId,
  status: 'running',
  phase: 'running',
  paused: null,
  end: null,
  startedAt: 1,
  reason: null,
};

/** The actions in order, from `state`. */
export function step(state: RunPageState, actions: readonly RunPageAction[]): RunPageState {
  let current = state;
  for (const action of actions) current = runPageStep(current, action).state;
  return current;
}

/** The page opened on the live run: what it asked for, before any answer. */
export function opened(): {
  readonly state: RunPageState;
  readonly requests: readonly RunPageRequest[];
  /** The answer that attaches the window to the run's session. */
  readonly attached: RunPageAction;
  /** The token the summary read was asked under. */
  readonly summaryToken: number;
} {
  const initial = runPageInitial({
    identity: RUN,
    title: {project: '~/project', task: 'serve'},
    hostLabel: 'host-a',
    unavailable: {},
  });
  const first = runPageStep(initial, {type: 'record', record: {kind: 'live', run: HOST_RUN}});
  const attach = first.requests.find(request => request.kind === 'attach');
  const summary = first.requests.find(request => request.kind === 'read-summary');
  if (attach?.kind !== 'attach') throw new Error('no attachment requested');
  if (summary?.kind !== 'read-summary') throw new Error('no summary requested');
  return {
    state: first.state,
    requests: first.requests,
    attached: {type: 'attached', token: attach.token, error: null},
    summaryToken: summary.token,
  };
}

/** The page attached to the live run, with nothing folded yet. */
export function attached(): RunPageState {
  const page = opened();
  return runPageStep(page.state, page.attached).state;
}

export function isoAt(seconds: number): string {
  return new Date(seconds * 1000).toISOString();
}

/** An idea (a workstream span) that began at `startedAt` and is still running. */
export function idea(startedAt: number): Span {
  return {
    id: 'flow:idea/#1',
    parent: null,
    type: 'flow',
    name: 'idea',
    status: 'running',
    started_at: startedAt,
    label: {kind: 'workstream', title: 'Try a ring buffer'},
  } as Span;
}

/**
 * A span event stamped `at` (epoch seconds on the run's host). The span's own times are on the run
 * clock, which starts wherever the run's host puts it: the caller builds the span.
 */
export function spanEvent(
  sequence: number,
  at: number,
  type: 'span_started' | 'span_updated' | 'span_ended',
  span: Span,
): RunEvent {
  return {
    protocol_version: 1,
    run_id: RUN.runId,
    sequence,
    timestamp: isoAt(at),
    type,
    data: {kind: type, span},
  } as RunEvent;
}

/** An event that changes no span (a status change, a control): only its timestamp says when. */
export function control(sequence: number, at: number): RunEvent {
  const type: string = 'control';
  return {
    protocol_version: 1,
    run_id: RUN.runId,
    sequence,
    timestamp: isoAt(at),
    type,
    data: {kind: type},
  } as RunEvent;
}

/** A gate of the input that finished failed, before the first idea. */
export function failedGate(sequence: number, gate: string, tail: string): RunEvent {
  return {
    protocol_version: 1,
    run_id: RUN.runId,
    sequence,
    timestamp: isoAt(T0 + sequence),
    type: 'gate_finished',
    status: 'failed',
    data: {kind: 'gate_finished', gate, recipe: null, output_tail: tail},
  } as RunEvent;
}

export function events(wallMs: number, batch: readonly RunEvent[]): RunPageAction {
  return {type: 'run-data', wallMs, data: {kind: 'events', events: batch}};
}
