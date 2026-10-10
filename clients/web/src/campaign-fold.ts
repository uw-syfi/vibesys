/**
 * The pure functional core of the live campaign: `state + frame -> state`.
 *
 * No I/O, no clock, no React. The shell (`useLiveCampaign`) owns the
 * subscription and the clock and calls into here. Keeping the fold pure is what
 * lets the whole live path be tested with generated frame sequences and no
 * sleeps (see `campaign-fold.test.ts`).
 */

import type {CampaignHeader, FrameAgent, FrameMeasurement} from './campaign-frames.js';
import {type CampaignFrame, type FrameWorkstream, isTerminalPhase} from './campaign-frames.js';
import type {CampaignRecord} from './campaign-record.js';

export interface FoldState {
  /** Sequence of the last applied frame; frames at or below it are duplicates. */
  readonly seq: number;
  readonly header: CampaignHeader | null;
  readonly workstreams: ReadonlyMap<string, FrameWorkstream>;
  readonly agents: ReadonlyMap<string, FrameAgent>;
  readonly measurements: ReadonlyMap<string, FrameMeasurement>;
  readonly tokenSpend: ReadonlyMap<string, number>;
  readonly status: 'active' | 'completed';
}

export function initialFoldState(): FoldState {
  return {
    seq: 0,
    header: null,
    workstreams: new Map(),
    agents: new Map(),
    measurements: new Map(),
    tokenSpend: new Map(),
    status: 'active',
  };
}

function withEntry<V>(map: ReadonlyMap<string, V>, key: string, value: V): ReadonlyMap<string, V> {
  const next = new Map(map);
  next.set(key, value);
  return next;
}

/**
 * Apply one frame. Frames at or below the last applied sequence are ignored, so
 * a reconnect that replays the log is idempotent. Upserts merge by id; token
 * frames carry the cumulative total, so applying one twice is a no-op.
 */
export function foldCampaignFrame(state: FoldState, frame: CampaignFrame): FoldState {
  if (frame.seq <= state.seq) return state;
  const seq = frame.seq;
  switch (frame.kind) {
    case 'campaign-init':
      return {...state, seq, header: frame.header};
    case 'agent-upsert':
      return {...state, seq, agents: withEntry(state.agents, frame.agent.id, frame.agent)};
    case 'workstream-upsert':
      return {
        ...state,
        seq,
        workstreams: withEntry(state.workstreams, frame.workstream.id, frame.workstream),
      };
    case 'measurement':
      return {
        ...state,
        seq,
        measurements: withEntry(state.measurements, frame.measurement.id, frame.measurement),
      };
    case 'tokens':
      return {
        ...state,
        seq,
        tokenSpend: withEntry(state.tokenSpend, frame.workstreamId, frame.tokens),
      };
    case 'status':
      return {...state, seq, status: frame.status};
  }
}

export function foldFrames(frames: Iterable<CampaignFrame>): FoldState {
  let state = initialFoldState();
  for (const frame of frames) state = foldCampaignFrame(state, frame);
  return state;
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

/** The live "now" in epoch ms: the latest of every measurement timestamp and
 * every workstream start, else 0. Used to end in-flight workstream bars at the
 * current moment. Must consider workstream starts unconditionally, not only
 * when there are no measurements yet: a workstream can start after the latest
 * measurement elsewhere in the run, and a one-sided fallback would then place
 * "now" before that workstream's own start. */
export function latestMoment(state: FoldState): number {
  let moment = 0;
  for (const measurement of state.measurements.values())
    moment = Math.max(moment, Date.parse(measurement.timestamp));
  for (const workstream of state.workstreams.values())
    moment = Math.max(moment, Date.parse(workstream.startedAt));
  return moment;
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
