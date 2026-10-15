/**
 * The live campaign stream: a `CampaignStream` backed by a real `WebSession` instead of the
 * dev-fixture replay server. `webSessionCampaignStream` owns only the subscription (it reads the
 * session's already-folded `CoreState` and republishes it as campaign frames); the actual
 * `CoreState -> CampaignFrame[]` translation is the pure, independently testable
 * `projectCampaignFrames` below.
 *
 * `CoreState` is not itself a campaign: most `CampaignRecord` fields (the objective, the
 * benchmark-version catalog, per-agent trajectories, a workstream's hypothesis) are reported
 * through wire paths this client does not fold today (see the module doc comments on
 * `campaign-frames.ts` and `docs/design/live-campaign-streaming.md`). This projection reports
 * only what `CoreState` actually knows and leaves the rest as the documented, empty defaults
 * `CampaignDashboard` already renders gracefully for a fixture with no such data.
 *
 * `CampaignFoldState` (`campaign-fold.ts`) requires the frame sequence to start at 1 with
 * `campaign-init` and stay perfectly contiguous thereafter, and treats a `status: 'completed'`
 * frame as absorbing: no frame may follow it. `projectCampaignFrames` keeps one monotonic counter
 * across every frame kind to satisfy the first requirement, and once it reports `completed` it
 * reports no further frames ever again, by construction, to satisfy the second.
 */

import type {WebSession} from '@vibesys/run-session';
import type {CoreState} from '@vibesys/run-state';
import {hasRunEnded} from '@vibesys/run-state';
import {type RoundKey, type RoundState, sameRoundKey} from '@vibesys/run-state/rounds';
import type {
  CampaignFrame,
  CampaignHeader,
  FrameAgent,
  FrameMeasurement,
  FrameWorkstream,
  WorkstreamPhase,
} from './campaign-frames.js';
import type {MeasurementDisposition} from './campaign-record.js';
import type {CampaignStream, FrameListener, StreamErrorListener} from './campaign-stream.js';

/** Placeholder workstream id for a benchmark observation with no round scope at all. */
const UNSCOPED_WORKSTREAM_ID = 'unscoped';

/**
 * What `projectCampaignFrames` has already reported, so a later call reports only what changed.
 * Opaque to callers; obtain the first value from `initialCampaignProjection`.
 */
export interface CampaignProjectionState {
  readonly seq: number;
  readonly headerSent: boolean;
  /** Once true, every future call reports no frames: the stream has told the fold it ended. */
  readonly completed: boolean;
  /** Per agent kind, the comma-joined workstream ids last reported for it. */
  readonly agentSignatures: Readonly<Record<string, string>>;
  /** Per workstream id, a canonical form of the mutable fields last reported for it. */
  readonly workstreamSignatures: Readonly<Record<string, string>>;
  /** Per workstream id, the cumulative token total last reported for it. */
  readonly sentTokens: Readonly<Record<string, number>>;
  /** Benchmark-record sequences already reported as a measurement frame. */
  readonly sentMeasurementSequences: Readonly<Record<number, true>>;
}

export function initialCampaignProjection(): CampaignProjectionState {
  return {
    seq: 0,
    headerSent: false,
    completed: false,
    agentSignatures: {},
    workstreamSignatures: {},
    sentTokens: {},
    sentMeasurementSequences: {},
  };
}

type SeqAllocator = () => number;

/** One projection step: every frame `core` has newly earned since `state`, and the state to pass
 * to the next call. Pure; takes no session or subscription of its own. */
export function projectCampaignFrames(
  state: CampaignProjectionState,
  core: CoreState,
): {readonly state: CampaignProjectionState; readonly frames: readonly CampaignFrame[]} {
  if (state.completed) return {state, frames: []};

  const frames: CampaignFrame[] = [];
  let seq = state.seq;
  const nextSeq: SeqAllocator = () => {
    seq += 1;
    return seq;
  };

  let headerSent = state.headerSent;
  if (!headerSent) {
    frames.push({kind: 'campaign-init', seq: nextSeq(), header: liveHeader(core)});
    headerSent = true;
  }

  const agentSignatures = collectAgentFrames(core, state.agentSignatures, nextSeq, frames);
  const {workstreamSignatures, sentTokens} = collectWorkstreamFrames(
    core,
    state.workstreamSignatures,
    state.sentTokens,
    nextSeq,
    frames,
  );
  const sentMeasurementSequences = collectMeasurementFrames(
    core,
    state.sentMeasurementSequences,
    nextSeq,
    frames,
  );

  const completed = hasRunEnded(core);
  if (completed) frames.push({kind: 'status', seq: nextSeq(), status: 'completed'});

  return {
    state: {
      seq,
      headerSent,
      completed,
      agentSignatures,
      workstreamSignatures,
      sentTokens,
      sentMeasurementSequences,
    },
    frames,
  };
}

/** Every agent-kind roster change, appended to `frames`; the signatures to remember next. */
function collectAgentFrames(
  core: CoreState,
  previous: Readonly<Record<string, string>>,
  nextSeq: SeqAllocator,
  frames: CampaignFrame[],
): Readonly<Record<string, string>> {
  const agentSignatures = {...previous};
  for (const [kind, workstreamIds] of buildAgentCatalog(core)) {
    const signature = workstreamIds.join(',');
    if (agentSignatures[kind] === signature) continue;
    agentSignatures[kind] = signature;
    const agent: FrameAgent = {id: kind, name: humanize(kind), role: kind, workstreamIds};
    frames.push({kind: 'agent-upsert', seq: nextSeq(), agent});
  }
  return agentSignatures;
}

/** Every workstream and token-total change, appended to `frames`; the facts to remember next. */
function collectWorkstreamFrames(
  core: CoreState,
  previousWorkstreams: Readonly<Record<string, string>>,
  previousTokens: Readonly<Record<string, number>>,
  nextSeq: SeqAllocator,
  frames: CampaignFrame[],
): {
  readonly workstreamSignatures: Readonly<Record<string, string>>;
  readonly sentTokens: Readonly<Record<string, number>>;
} {
  const workstreamSignatures = {...previousWorkstreams};
  const sentTokens = {...previousTokens};
  for (const round of core.rounds) {
    const workstream = workstreamFor(round, core);
    if (workstream === null) continue;
    const signature = workstreamSignature(workstream);
    if (workstreamSignatures[workstream.id] !== signature) {
      workstreamSignatures[workstream.id] = signature;
      frames.push({kind: 'workstream-upsert', seq: nextSeq(), workstream});
    }

    const record = findWorkstreamRecord(core.workstreams, round.key);
    const tokens = record === null ? 0 : totalTokens(record.usage);
    if (tokens > 0 && sentTokens[workstream.id] !== tokens) {
      sentTokens[workstream.id] = tokens;
      frames.push({kind: 'tokens', seq: nextSeq(), workstreamId: workstream.id, tokens});
    }
  }
  return {workstreamSignatures, sentTokens};
}

/** Every not-yet-reported benchmark observation, appended to `frames` as a measurement. */
function collectMeasurementFrames(
  core: CoreState,
  previous: Readonly<Record<number, true>>,
  nextSeq: SeqAllocator,
  frames: CampaignFrame[],
): Readonly<Record<number, true>> {
  const sentMeasurementSequences = {...previous};
  for (const benchmark of core.benchmarks) {
    if (sentMeasurementSequences[benchmark.sequence]) continue;
    sentMeasurementSequences[benchmark.sequence] = true;
    const measurement = measurementFor(benchmark, core);
    frames.push({kind: 'measurement', seq: nextSeq(), measurement});
  }
  return sentMeasurementSequences;
}

/**
 * A `CampaignStream` that reads one `WebSession`'s already-folded `CoreState`.
 *
 * Does not subscribe to the session's transport: `RunConnection.subscribe` is single-subscriber
 * and the session itself already owns that subscription. This reads `session.store`, which
 * supports multiple independent subscribers, so a live campaign view can coexist with the run
 * page's own subscription to the same session.
 */
export function webSessionCampaignStream(session: WebSession): CampaignStream {
  return {
    subscribe(onFrame: FrameListener, onError: StreamErrorListener): () => void {
      let projection = initialCampaignProjection();
      const step = (core: CoreState): void => {
        try {
          const result = projectCampaignFrames(projection, core);
          projection = result.state;
          for (const frame of result.frames) onFrame(frame);
        } catch (error) {
          onError(error instanceof Error ? error : new Error(String(error)));
        }
      };
      step(session.store.getState());
      return session.store.subscribe(() => step(session.store.getState()));
    },
  };
}

/**
 * The campaign header this client can honestly report from a live `CoreState`: identity from the
 * run itself, and empty objective/benchmark-catalog/trajectory data, since none of that is folded
 * from the `RunEvent` stream today. `CampaignDashboard` already renders these fields empty for a
 * fixture that declares none, the same tolerance this relies on.
 */
function liveHeader(core: CoreState): CampaignHeader {
  const id = core.runId ?? 'live';
  return {
    id,
    title: `Run ${id}`,
    summary: '',
    provenance: 'live-run',
    objective: {title: '', statement: '', target: null, constraints: [], gates: [], metrics: []},
    benchmarkVersions: [],
    benchmarkVersionBoundary: {fromVersion: '', toVersion: '', afterSequence: 0, reason: ''},
    trajectories: [],
  };
}

/** Distinct agent kinds with at least one round scope, each mapped to its sorted workstream ids. */
function buildAgentCatalog(core: CoreState): Map<string, string[]> {
  const catalog = new Map<string, Set<string>>();
  for (const phase of core.phases) {
    if (phase.roundKey === null) continue;
    const ids = catalog.get(phase.kind) ?? new Set<string>();
    ids.add(idForRoundKey(phase.roundKey));
    catalog.set(phase.kind, ids);
  }
  const result = new Map<string, string[]>();
  for (const [kind, ids] of catalog) result.set(kind, [...ids].sort());
  return result;
}

function workstreamSignature(workstream: FrameWorkstream): string {
  return JSON.stringify({
    phase: workstream.phase,
    active: workstream.active,
    finishedAt: workstream.finishedAt,
    lastSequence: workstream.lastSequence,
    outcome: workstream.outcome,
    outcomeSummary: workstream.outcomeSummary,
  });
}

type WorkstreamBase = Pick<
  FrameWorkstream,
  'id' | 'title' | 'hypothesis' | 'startedAt' | 'firstSequence'
>;
type WorkstreamRecordLike = NonNullable<ReturnType<typeof findWorkstreamRecord>>;

/** The workstream `round` projects to, or null while it has no start time yet (a planned round
 * the run has not reached): a workstream with no start cannot be reported, since every consumer
 * of `FrameWorkstream.startedAt` treats it as a real timestamp. */
function workstreamFor(round: RoundState, core: CoreState): FrameWorkstream | null {
  if (round.startedAt === undefined) return null;
  const base: WorkstreamBase = {
    id: idForRoundKey(round.key),
    title: round.iterationLabel ?? titleForRoundKey(round.key),
    hypothesis: '',
    startedAt: round.startedAt,
    firstSequence: 1,
  };
  const record = findWorkstreamRecord(core.workstreams, round.key);
  return record?.finished
    ? finishedWorkstream(base, record)
    : activeOrSettledWorkstream(base, round);
}

/** `workstream_finished` (or `turn_usage_recorded`) already settled this round: its own terminal
 * facts take priority over the run map's coarser round status. */
function finishedWorkstream(base: WorkstreamBase, record: WorkstreamRecordLike): FrameWorkstream {
  const failed = record.lastError !== null;
  return {
    ...base,
    phase: failed ? 'failed' : 'evaluated',
    active: false,
    finishedAt: record.finishedAt,
    lastSequence: record.finishedAtSequence,
    outcome: failed ? 'rejected' : 'accepted',
    outcomeSummary: failed
      ? (nonEmpty(record.lastError?.text) ?? 'Workstream failed')
      : 'Workstream completed',
  };
}

/** No `workstream_finished` has arrived (a non-dynamic loop kind that never emits one, or one
 * that simply has not finished yet): fall back to the run map's own round status. */
function activeOrSettledWorkstream(base: WorkstreamBase, round: RoundState): FrameWorkstream {
  if (round.status === 'failed') {
    return settledWorkstream(base, round, 'failed', 'rejected', 'Round failed');
  }
  if (round.status === 'completed') {
    return settledWorkstream(base, round, 'evaluated', 'accepted', 'Round completed');
  }
  // 'active', and the defensive fallback for 'planned' (a planned round is not expected to carry
  // a startedAt, but one that does is in progress, not yet outcome-bearing, same as 'active').
  return {
    ...base,
    phase: 'implementing' satisfies WorkstreamPhase,
    active: true,
    finishedAt: null,
    lastSequence: null,
    outcome: null,
    outcomeSummary: '',
  };
}

function settledWorkstream(
  base: WorkstreamBase,
  round: RoundState,
  phase: WorkstreamPhase,
  outcome: NonNullable<FrameWorkstream['outcome']>,
  outcomeSummary: string,
): FrameWorkstream {
  return {
    ...base,
    phase,
    active: false,
    finishedAt: round.finishedAt ?? base.startedAt,
    lastSequence: null,
    outcome,
    outcomeSummary,
  };
}

function nonEmpty(text: string | undefined): string | null {
  return text !== undefined && text.length > 0 ? text : null;
}

function totalTokens(usage: {
  readonly inputTokens: number;
  readonly cacheCreationInputTokens: number;
  readonly cacheReadInputTokens: number;
  readonly outputTokens: number;
}): number {
  // total_cost_usd is folded (see `workstream.ts`) but has no CampaignFrame field to carry it to;
  // only the token counts, which this dashboard's tokens frame reports, are summed here.
  return (
    usage.inputTokens +
    usage.cacheCreationInputTokens +
    usage.cacheReadInputTokens +
    usage.outputTokens
  );
}

function measurementFor(
  benchmark: CoreState['benchmarks'][number],
  core: CoreState,
): FrameMeasurement {
  const workstreamId =
    benchmark.roundKey === null ? UNSCOPED_WORKSTREAM_ID : idForRoundKey(benchmark.roundKey);
  return {
    id: `benchmark-${benchmark.sequence}`,
    sequence: benchmark.sequence,
    timestamp: benchmark.timestamp,
    workstreamId,
    label: benchmark.metric,
    // CoreState's benchmark fold does not distinguish an official measurement from a candidate,
    // control, diagnostic, or quick one; 'official' is the least speculative single value to
    // report for a kind this client cannot currently tell apart.
    measurementKind: 'official',
    sourceOrder: null,
    triggeredByAgentId: null,
    runnerAgentId: null,
    benchmarkVersion: '',
    values: [{metricId: benchmark.metric, value: benchmark.value, unit: benchmark.unit}],
    gates: [],
    disposition: dispositionFor(benchmark, core),
  };
}

function dispositionFor(
  benchmark: CoreState['benchmarks'][number],
  core: CoreState,
): MeasurementDisposition {
  if (benchmark.roundKey === null) return 'inconclusive';
  const record = findWorkstreamRecord(core.workstreams, benchmark.roundKey);
  if (record?.finished) {
    return record.lastError !== null ? 'rejected' : 'accepted';
  }
  const round = core.rounds.find(candidate => sameRoundKey(candidate.key, benchmark.roundKey));
  if (round?.status === 'failed') return 'rejected';
  if (round?.status === 'completed') return 'accepted';
  return 'inconclusive';
}

function findWorkstreamRecord(
  workstreams: CoreState['workstreams'],
  key: RoundKey,
): CoreState['workstreams'][string] | null {
  for (const record of Object.values(workstreams)) {
    if (sameRoundKey(record.roundKey, key)) return record;
  }
  return null;
}

function titleForRoundKey(key: RoundKey): string {
  return key.kind === 'number' ? `Round ${key.number}` : key.label;
}

/** Presentation-facing workstream id. Independent of (and not string-compatible with) the
 * run-state-internal dictionary key `workstream.ts` uses for the same `RoundKey`; the two are
 * correlated only by `sameRoundKey`, never by a shared string format. */
function idForRoundKey(key: RoundKey): string {
  return key.kind === 'number' ? `round-${key.number}` : `round-label-${key.label}`;
}

function humanize(kind: string): string {
  const words = kind
    .split(/[_-]+/)
    .filter(word => word.length > 0)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1));
  return words.length > 0 ? words.join(' ') : kind;
}
