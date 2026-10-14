/**
 * The pure functional core of the live campaign: `state + frame -> state`.
 *
 * No I/O, no clock, no React. The campaign store owns the
 * subscription and calls into here. Keeping the fold pure is what
 * lets the whole live path be tested with generated frame sequences and no
 * sleeps (see `campaign-fold.test.ts`).
 */

import type {CampaignHeader, FrameAgent, FrameMeasurement} from './campaign-frames.js';
import {type CampaignFrame, type FrameWorkstream, isTerminalPhase} from './campaign-frames.js';
import type {CampaignRecord} from './campaign-record.js';

interface ObservedFrameIdentity {
  readonly kind: CampaignFrame['kind'];
  readonly canonical: string;
}

/** Kept private so callers cannot manufacture a partially witnessed prefix. */
const observedFrames: unique symbol = Symbol('campaign observed frames');

export interface FoldState {
  /** Sequence of the last applied frame. Replayed sequences must match their original payload. */
  readonly seq: number;
  readonly header: CampaignHeader | null;
  readonly workstreams: ReadonlyMap<string, FrameWorkstream>;
  readonly agents: ReadonlyMap<string, FrameAgent>;
  readonly measurements: ReadonlyMap<string, FrameMeasurement>;
  readonly tokenSpend: ReadonlyMap<string, number>;
  /** Greatest timestamp observed on any applied measurement or workstream frame. */
  readonly observationHorizon: number;
  readonly status: 'active' | 'completed';
  readonly [observedFrames]: ReadonlyMap<number, ObservedFrameIdentity>;
}

/** Why a frame cannot extend the authoritative prefix accumulated so far. */
export type CampaignFoldError =
  | {
      readonly kind: 'init-required';
      readonly actualKind: CampaignFrame['kind'];
      readonly actualSeq: number;
    }
  | {readonly kind: 'initial-sequence'; readonly actualSeq: number}
  | {readonly kind: 'sequence-gap'; readonly expectedSeq: number; readonly actualSeq: number}
  | {
      readonly kind: 'sequence-conflict';
      readonly actualSeq: number;
      readonly observedKind: CampaignFrame['kind'];
      readonly receivedKind: CampaignFrame['kind'];
    }
  | {
      readonly kind: 'unobserved-sequence';
      readonly actualSeq: number;
      readonly receivedKind: CampaignFrame['kind'];
    }
  | {readonly kind: 'unexpected-init'; readonly actualSeq: number}
  | {
      readonly kind: 'campaign-completed';
      readonly actualKind: CampaignFrame['kind'];
      readonly actualSeq: number;
    };

/**
 * A frame either extends the authoritative prefix, exactly duplicates a
 * witnessed frame, or is rejected without changing the prefix. The shell
 * decides how a rejected stream affects its control lifecycle.
 */
export type CampaignFoldOutcome =
  | {readonly kind: 'applied'; readonly state: FoldState}
  | {readonly kind: 'duplicate'; readonly state: FoldState}
  | {readonly kind: 'rejected'; readonly state: FoldState; readonly error: CampaignFoldError};

export function initialFoldState(): FoldState {
  return {
    seq: 0,
    header: null,
    workstreams: new Map(),
    agents: new Map(),
    measurements: new Map(),
    tokenSpend: new Map(),
    observationHorizon: 0,
    status: 'active',
    [observedFrames]: new Map(),
  };
}

function withEntry<V>(map: ReadonlyMap<string, V>, key: string, value: V): ReadonlyMap<string, V> {
  const next = new Map(map);
  next.set(key, value);
  return next;
}

function workstreamHorizon(workstream: FrameWorkstream): number {
  const startedAt = Date.parse(workstream.startedAt);
  return workstream.finishedAt === null
    ? startedAt
    : Math.max(startedAt, Date.parse(workstream.finishedAt));
}

function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('campaign frame identity requires finite numbers');
    return Object.is(value, -0) ? '-0' : String(value);
  }
  if (typeof value === 'undefined') return 'undefined';
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (typeof value === 'object') {
    const object = value as Readonly<Record<string, unknown>>;
    return `{${Object.keys(object)
      .sort()
      .map(key => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
      .join(',')}}`;
  }
  throw new Error(`campaign frame identity does not support ${typeof value}`);
}

function frameIdentity(frame: CampaignFrame): ObservedFrameIdentity {
  return {kind: frame.kind, canonical: canonicalJson(frame)};
}

function applied(previous: FoldState, frame: CampaignFrame, next: FoldState): CampaignFoldOutcome {
  const nextObserved = new Map(previous[observedFrames]);
  nextObserved.set(frame.seq, frameIdentity(frame));
  return {kind: 'applied', state: {...next, [observedFrames]: nextObserved}};
}

function duplicate(state: FoldState): CampaignFoldOutcome {
  return {kind: 'duplicate', state};
}

function rejected(state: FoldState, error: CampaignFoldError): CampaignFoldOutcome {
  return {kind: 'rejected', state, error};
}

export function campaignFoldErrorMessage(error: CampaignFoldError): string {
  switch (error.kind) {
    case 'init-required':
      return `campaign stream must begin with campaign-init at sequence 1; received ${error.actualKind} at sequence ${error.actualSeq}`;
    case 'initial-sequence':
      return `campaign-init must have sequence 1; received ${error.actualSeq}`;
    case 'sequence-gap':
      return `campaign stream sequence gap: expected ${error.expectedSeq}, received ${error.actualSeq}`;
    case 'sequence-conflict':
      return `campaign stream sequence ${error.actualSeq} conflicts with its observed ${error.observedKind} frame; received ${error.receivedKind}`;
    case 'unobserved-sequence':
      return `campaign stream sequence ${error.actualSeq} was never observed; received ${error.receivedKind}`;
    case 'unexpected-init':
      return `campaign stream already initialized; received campaign-init at sequence ${error.actualSeq}`;
    case 'campaign-completed':
      return `campaign stream is complete; received ${error.actualKind} at sequence ${error.actualSeq}`;
  }
}

function replayOutcome(state: FoldState, frame: CampaignFrame): CampaignFoldOutcome | null {
  if (state.header === null || frame.seq > state.seq) return null;
  const observed = state[observedFrames].get(frame.seq);
  if (observed === undefined)
    return rejected(state, {
      kind: 'unobserved-sequence',
      actualSeq: frame.seq,
      receivedKind: frame.kind,
    });
  const received = frameIdentity(frame);
  if (observed.canonical === received.canonical) return duplicate(state);
  return rejected(state, {
    kind: 'sequence-conflict',
    actualSeq: frame.seq,
    observedKind: observed.kind,
    receivedKind: received.kind,
  });
}

function nextFrameError(state: FoldState, frame: CampaignFrame): CampaignFoldError | null {
  if (state.header === null) {
    if (frame.kind !== 'campaign-init')
      return {kind: 'init-required', actualKind: frame.kind, actualSeq: frame.seq};
    return frame.seq === 1 ? null : {kind: 'initial-sequence', actualSeq: frame.seq};
  }
  if (state.status === 'completed')
    return {kind: 'campaign-completed', actualKind: frame.kind, actualSeq: frame.seq};
  const expectedSeq = state.seq + 1;
  if (frame.seq !== expectedSeq) return {kind: 'sequence-gap', expectedSeq, actualSeq: frame.seq};
  return frame.kind === 'campaign-init' ? {kind: 'unexpected-init', actualSeq: frame.seq} : null;
}

function applyNewFrame(state: FoldState, frame: CampaignFrame): CampaignFoldOutcome {
  const seq = frame.seq;
  switch (frame.kind) {
    case 'campaign-init':
      return applied(state, frame, {...state, seq, header: frame.header});
    case 'agent-upsert':
      return applied(state, frame, {
        ...state,
        seq,
        agents: withEntry(state.agents, frame.agent.id, frame.agent),
      });
    case 'workstream-upsert':
      return applied(state, frame, {
        ...state,
        seq,
        workstreams: withEntry(state.workstreams, frame.workstream.id, frame.workstream),
        observationHorizon: Math.max(state.observationHorizon, workstreamHorizon(frame.workstream)),
      });
    case 'measurement':
      return applied(state, frame, {
        ...state,
        seq,
        measurements: withEntry(state.measurements, frame.measurement.id, frame.measurement),
        observationHorizon: Math.max(
          state.observationHorizon,
          Date.parse(frame.measurement.timestamp),
        ),
      });
    case 'tokens':
      return applied(state, frame, {
        ...state,
        seq,
        tokenSpend: withEntry(state.tokenSpend, frame.workstreamId, frame.tokens),
      });
    case 'status':
      return applied(state, frame, {...state, seq, status: frame.status});
  }
}

/**
 * Reconcile one frame with the authoritative prefix. Sequence one must be the
 * sole campaign-init, applied frames must be contiguous, exact replay is
 * idempotent, conflicting sequence reuse is rejected, and completion is
 * absorbing. Rejection never mutates the prefix.
 */
export function foldCampaignFrame(state: FoldState, frame: CampaignFrame): CampaignFoldOutcome {
  const replay = replayOutcome(state, frame);
  if (replay !== null) return replay;
  const error = nextFrameError(state, frame);
  return error === null ? applyNewFrame(state, frame) : rejected(state, error);
}

export function foldFrames(frames: Iterable<CampaignFrame>): CampaignFoldOutcome {
  let state = initialFoldState();
  let anyApplied = false;
  for (const frame of frames) {
    const outcome = foldCampaignFrame(state, frame);
    if (outcome.kind === 'rejected') return outcome;
    anyApplied ||= outcome.kind === 'applied';
    state = outcome.state;
  }
  return {kind: anyApplied ? 'applied' : 'duplicate', state};
}

function sortedMeasurements(
  measurements: ReadonlyMap<string, FrameMeasurement>,
): CampaignRecord['measurements'] {
  return [...measurements.values()].sort((left, right) => left.sequence - right.sequence);
}

function projectWorkstream(
  workstream: FrameWorkstream,
  nowIso: string,
): CampaignRecord['workstreams'][number] {
  const live =
    workstream.active || !isTerminalPhase(workstream.phase) || workstream.finishedAt === null;
  return {
    id: workstream.id,
    title: workstream.title,
    hypothesis: workstream.hypothesis,
    startedAt: workstream.startedAt,
    // An in-flight workstream ends "now", so the existing dashboard renders it
    // active (it compares the cursor to finishedAt). A finished one uses its
    // real end. The placeholder outcome below is unused while active.
    finishedAt: live ? nowIso : (workstream.finishedAt ?? nowIso),
    firstSequence: workstream.firstSequence,
    lastSequence: workstream.lastSequence ?? workstream.firstSequence,
    outcome: workstream.outcome ?? 'rejected',
    outcomeSummary: workstream.outcomeSummary,
  };
}

/** The live observation horizon in epoch ms: the latest timestamp carried by a
 * measurement or workstream transition, else 0. Used to end in-flight
 * workstream bars at the current observed moment and to project the live
 * cursor independently of measurement selection. */
export function latestMoment(state: FoldState): number {
  return state.observationHorizon;
}

/**
 * Project the accumulated state into the finished-record shape the dashboard
 * consumes, with in-flight workstreams ending one second after `now` so they
 * render as active. Returns null until the campaign header has arrived.
 */
export function foldStateToRecord(state: FoldState, now: number): CampaignRecord | null {
  if (state.header === null) return null;
  const nowIso = new Date(now + 1000).toISOString();
  return {
    schemaVersion: 1,
    id: state.header.id,
    title: state.header.title,
    summary: state.header.summary,
    provenance: state.header.provenance,
    architecture: null,
    objective: state.header.objective,
    benchmarkVersions: state.header.benchmarkVersions,
    benchmarkVersionBoundary: state.header.benchmarkVersionBoundary,
    workstreams: [...state.workstreams.values()].map(workstream =>
      projectWorkstream(workstream, nowIso),
    ),
    agents: [...state.agents.values()],
    measurements: sortedMeasurements(state.measurements),
    trajectories: state.header.trajectories,
  };
}
