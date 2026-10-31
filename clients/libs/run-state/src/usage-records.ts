/**
 * The usage records of a run's events: one `UsageRecord` per finished agent turn, the client's own
 * fold of the journal that the host's `runs.summary` serves the same records of
 * (`server.run_usage`). A `turn_usage_recorded` event says what a turn spent; the
 * `agent_execution_started` event with the same execution id says who ran it (role, harness,
 * model). The fold joins them as events arrive, so a live run and a journal read to its end have
 * the records an ended run's summary has.
 *
 * A value the journal does not hold is `null`, never a guess: a turn whose execution start was
 * never recorded still has a record, with no role, harness or model.
 */
import type {RunEvent} from '@vibesys/contracts';
import type {ModelUsage, UsageRecord} from '@vibesys/contracts/host-helper';

/** What an `agent_execution_started` event says about who ran a turn. */
interface ExecutionStart {
  readonly role: string | null;
  readonly harness: string | null;
  readonly model: string | null;
  readonly client: 'scripted' | 'provider' | null;
  readonly reasoningEffort: string | null;
  readonly harnessVersion: string | null;
  readonly startedAt: string | null;
}

/** The execution starts seen so far, by execution id. */
export type ExecutionStarts = Readonly<Record<string, ExecutionStart>>;

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

function cost(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function time(value: unknown): string | null {
  return typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null;
}

type RecordedCostBasis = NonNullable<UsageRecord['cost_basis']>;
type UsageSpeed = NonNullable<ModelUsage['speed']>;

const COST_BASES: ReadonlySet<unknown> = new Set<RecordedCostBasis>([
  'price_table',
  'provider_reported',
  'unknown_model',
  'not_reported',
]);
const SPEEDS: ReadonlySet<unknown> = new Set<UsageSpeed>(['standard', 'fast', 'ultrafast']);

function costBasis(value: unknown): RecordedCostBasis | null {
  return COST_BASES.has(value) ? (value as RecordedCostBasis) : null;
}

/** One model's share of a turn, or null when the row is malformed. */
function modelRow(value: unknown): ModelUsage | null {
  if (typeof value !== 'object' || value === null) return null;
  const row = value as Record<string, unknown>;
  const model = text(row['model']);
  const input = count(row['input_tokens']);
  const cacheRead = count(row['cache_read_input_tokens']);
  const cacheWrite = count(row['cache_creation_input_tokens']);
  const cacheWrite1h = count(row['cache_creation_1h_input_tokens']);
  const output = count(row['output_tokens']);
  const reasoning = count(row['reasoning_output_tokens']);
  const turns = count(row['turns']);
  const primary = row['primary'];
  if (
    model === null ||
    input === null ||
    cacheRead === null ||
    cacheWrite === null ||
    cacheWrite1h === null ||
    output === null ||
    reasoning === null ||
    turns === null ||
    typeof primary !== 'boolean'
  ) {
    return null;
  }
  return {
    model,
    input,
    cache_read: cacheRead,
    cache_write: cacheWrite,
    cache_write_1h: cacheWrite1h,
    output,
    reasoning,
    turns,
    recorded_usd: cost(row['recorded_usd']),
    speed: SPEEDS.has(row['speed']) ? (row['speed'] as UsageSpeed) : null,
    primary,
  };
}

/**
 * The rows a `by_model` holds; none when the line has no split. A malformed row makes the whole
 * split unreported: the rows left would sum to less than the turn, and a partial split reads as
 * complete.
 */
function modelRows(value: unknown): ModelUsage[] {
  if (!Array.isArray(value)) return [];
  const rows: ModelUsage[] = [];
  for (const item of value) {
    const row = modelRow(item);
    if (row === null) return [];
    rows.push(row);
  }
  return rows;
}

/** `starts` with the execution `event` starts, when it is one. */
export function startsWith(starts: ExecutionStarts, event: RunEvent): ExecutionStarts {
  const data = event.data;
  const id = event.execution_id;
  if (data?.kind !== 'agent_execution_started' || id == null) return starts;
  const client = data.client === 'scripted' || data.client === 'provider' ? data.client : null;
  return {
    ...starts,
    [id]: {
      role: text(event.agent_kind),
      harness: text(data.provider),
      model: text(data.model),
      client,
      reasoningEffort: text(data.reasoning_effort),
      harnessVersion: text(data.harness_version),
      startedAt: time(event.timestamp),
    },
  };
}

/** The record of the turn a `turn_usage_recorded` event reports, joined to its start. */
export function recordOf(starts: ExecutionStarts, event: RunEvent): UsageRecord | null {
  const data = event.data;
  if (data?.kind !== 'turn_usage_recorded') return null;
  const execution = text(event.execution_id);
  const start = execution === null ? undefined : starts[execution];
  return {
    run_id: text(event.run_id),
    execution_id: execution,
    span_id: text(data.span_id),
    round_label: text(event.round_label),
    role: start?.role ?? null,
    harness: start?.harness ?? null,
    model: start?.model ?? null,
    client: start?.client ?? null,
    reasoning_effort: start?.reasoningEffort ?? null,
    harness_version: start?.harnessVersion ?? null,
    started_at: start?.startedAt ?? null,
    ended_at: time(event.timestamp),
    tokens: {
      input: count(data.input_tokens),
      cache_read: count(data.cache_read_input_tokens),
      cache_write: count(data.cache_creation_input_tokens),
      output: count(data.output_tokens),
      reasoning: count(data.reasoning_output_tokens),
    },
    cost_usd: cost(data.total_cost_usd),
    duration_ms: count(data.duration_ms),
    cost_basis: costBasis(data.cost_basis),
    by_model: modelRows(data.by_model),
    provider_session_id: text(data.provider_session_id),
  };
}
