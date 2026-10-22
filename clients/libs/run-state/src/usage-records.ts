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
import type {UsageRecord} from '@vibesys/contracts/host-helper';

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

/** `starts` with the execution `event` starts, when it is one. */
export function startsWith(starts: ExecutionStarts, event: RunEvent): ExecutionStarts {
  const data = event.data;
  const id = event.execution_id;
  if (data?.kind !== 'agent_execution_started' || id == null) return starts;
  const client = data.client === 'scripted' || data.client === 'provider' ? data.client : null;
  const scripted = client === 'scripted';
  return {
    ...starts,
    [id]: {
      role: text(event.agent_kind),
      harness: scripted ? null : text(data.provider),
      model: scripted ? null : text(data.model),
      client,
      reasoningEffort: scripted ? null : text(data.reasoning_effort),
      harnessVersion: scripted ? null : text(data.harness_version),
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
  };
}
