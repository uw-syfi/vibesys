/**
 * Per-workstream (one dynamic-run round) telemetry folded from `workstream_finished` and
 * `turn_usage_recorded`. Neither event's payload carries its own workstream identity (see the
 * generated `WorkstreamFinishedData`/`TurnUsageData`): both are keyed by the event envelope's
 * `round_label`, the same scope `round-key.ts` already derives a `RoundKey` from, so this fold
 * reuses that identity rather than inventing a second one.
 *
 * The run-map fold (`run-map.ts`) already tracks a round's lifecycle (`started`/`finished`
 * timestamps, active/completed/failed status) from other events; this module only stores what
 * these two events themselves contribute (the terminal failure detail and settled token/cost
 * accounting), so a projection that wants a complete workstream view joins the two by `RoundKey`
 * (see `sameRoundKey`) instead of this module duplicating lifecycle bookkeeping.
 */
import type {RunEvent} from '@vibesys/contracts';
import {type RoundKey, roundKeyFor, roundNumberFor} from './round-key.js';

type WorkstreamFinishedData = Extract<NonNullable<RunEvent['data']>, {kind: 'workstream_finished'}>;
type TurnUsageData = Extract<NonNullable<RunEvent['data']>, {kind: 'turn_usage_recorded'}>;

/**
 * One workstream's typed account of why it ended without a measured candidate.
 *
 * Not exported by name: a consumer outside this module reads it structurally off
 * `CoreState['workstreams']` (the same precedent `BenchmarkRecord`'s neighbors follow), so there is
 * no public symbol to keep in sync here.
 */
interface WorkstreamFailure {
  readonly kind: NonNullable<WorkstreamFinishedData['last_error']>['kind'];
  readonly role: string | null;
  readonly turns: number | null;
  readonly limit: number | null;
  readonly text: string;
}

/** Settled token and cost accounting, summed over every turn folded for the workstream. */
interface WorkstreamUsage {
  readonly inputTokens: number;
  readonly cacheCreationInputTokens: number;
  readonly cacheReadInputTokens: number;
  readonly outputTokens: number;
  readonly totalCostUsd: number;
}

/** One workstream as `workstream_finished`/`turn_usage_recorded` alone describe it. */
interface WorkstreamRecord {
  readonly roundKey: RoundKey;
  readonly roundNumber: number | null;
  /** Raw backend `round_label` the identity was derived from. */
  readonly roundLabel: string | null;
  readonly finished: boolean;
  /** Timestamp of the `workstream_finished` event, or null before it arrives. */
  readonly finishedAt: string | null;
  /** Sequence of the `workstream_finished` event, or null before it arrives. */
  readonly finishedAtSequence: number | null;
  readonly repeatedError: boolean;
  readonly lastError: WorkstreamFailure | null;
  readonly usage: WorkstreamUsage;
}

export type WorkstreamMap = Readonly<Record<string, WorkstreamRecord>>;

const EMPTY_USAGE: WorkstreamUsage = {
  inputTokens: 0,
  cacheCreationInputTokens: 0,
  cacheReadInputTokens: 0,
  outputTokens: 0,
  totalCostUsd: 0,
};

/** Collision-free scalar key for one `RoundKey`, local to this module's dictionary. */
function keyToken(key: RoundKey): string {
  return key.kind === 'number' ? `number:${key.number}` : `label:${JSON.stringify(key.label)}`;
}

function emptyRecord(roundKey: RoundKey, roundLabel: string | null): WorkstreamRecord {
  return {
    roundKey,
    roundNumber: roundNumberFor(roundKey),
    roundLabel,
    finished: false,
    finishedAt: null,
    finishedAtSequence: null,
    repeatedError: false,
    lastError: null,
    usage: EMPTY_USAGE,
  };
}

function recordFor(workstreams: WorkstreamMap, event: RunEvent): WorkstreamRecord | null {
  const roundKey = roundKeyFor(event);
  if (roundKey === null) return null;
  return workstreams[keyToken(roundKey)] ?? emptyRecord(roundKey, event.round_label ?? null);
}

function failureFrom(data: WorkstreamFinishedData): WorkstreamFailure | null {
  const error = data.last_error;
  if (error === null || error === undefined) return null;
  return {
    kind: error.kind,
    role: error.role ?? null,
    turns: error.turns ?? null,
    limit: error.limit ?? null,
    text: error.text ?? '',
  };
}

/** The workstream map after one `workstream_finished` event, or `workstreams` unchanged when the
 * event carries no round scope to attach the fact to. */
export function workstreamsWithFinish(
  workstreams: WorkstreamMap,
  event: RunEvent,
  data: WorkstreamFinishedData,
  sequence: number,
): WorkstreamMap {
  const existing = recordFor(workstreams, event);
  if (existing === null) return workstreams;
  const next: WorkstreamRecord = {
    ...existing,
    finished: true,
    finishedAt: event.timestamp,
    finishedAtSequence: sequence,
    repeatedError: data.repeated_error,
    lastError: failureFrom(data),
  };
  return {...workstreams, [keyToken(next.roundKey)]: next};
}

/** The workstream map after one `turn_usage_recorded` event, accumulating its settled token and
 * cost fields onto the workstream the event's round scope already names, or `workstreams`
 * unchanged when the event carries no round scope. */
export function workstreamsWithUsage(
  workstreams: WorkstreamMap,
  event: RunEvent,
  data: TurnUsageData,
): WorkstreamMap {
  const existing = recordFor(workstreams, event);
  if (existing === null) return workstreams;
  const usage: WorkstreamUsage = {
    inputTokens: existing.usage.inputTokens + (data.input_tokens ?? 0),
    cacheCreationInputTokens:
      existing.usage.cacheCreationInputTokens + (data.cache_creation_input_tokens ?? 0),
    cacheReadInputTokens: existing.usage.cacheReadInputTokens + (data.cache_read_input_tokens ?? 0),
    outputTokens: existing.usage.outputTokens + (data.output_tokens ?? 0),
    totalCostUsd: existing.usage.totalCostUsd + (data.total_cost_usd ?? 0),
  };
  const next: WorkstreamRecord = {...existing, usage};
  return {...workstreams, [keyToken(next.roundKey)]: next};
}
