/**
 * Reads a recorded `run-events.jsonl` the way the server's read path reads it.
 *
 * `src/server/journal.py` canonicalizes a legacy journal when it is read rather
 * than rewriting the log, so a client never receives the recorded shape:
 *
 * - `RunEvent._execution_identity_compatibility` (`src/server/events.py`) fills
 *   `execution_id` from the legacy `invocation_id`, and the reverse. The two
 *   name one identity, minted once in `src/server/execution.py`.
 * - `_canonical_execution_events` (`src/server/journal.py`) rewrites
 *   `invocation_started` and `invocation_finished` into
 *   `agent_execution_started` and `agent_execution_finished`, and drops a legacy
 *   lifecycle event outright when the same execution also recorded a canonical
 *   one.
 *
 * Every client-facing read applies both, `checkpoint_locked` included, which is
 * the subscription checkpoint the TUI's `subscribe` consumes. Replaying a
 * fixture without them made the harness the one place a client meets a raw
 * legacy line: `applyAgentExecutionEvent` returns the state unchanged when
 * `execution_id` is null, so `bad-cpp-round1.jsonl`, the default fixture,
 * replayed with no agent executions at all.
 *
 * This is a hand port, because the harness is TypeScript and cannot import the
 * Python. `test_canonical_events_match_the_backend_read_path` in
 * `tests/server/test_tui_dev_harness.py` runs the real adapter over every
 * bundled fixture and writes `canonical-events.golden.json`; the parity test in
 * `harness.test.ts` holds this module to that same file. A change made on one
 * side and not the other fails one of the two.
 *
 * One branch of `_canonical_execution_events` is deliberately not ported: the
 * one that synthesizes a lifecycle event from `phase_started`/`phase_finished`
 * for an execution that recorded no lifecycle event of either spelling. No
 * bundled fixture reaches it, because every phase event in every one of them
 * shares its execution with a lifecycle event, and it is the only branch that
 * emits a second event at an already-used sequence, which this replay's
 * one-event-per-sequence stepping does not model.
 */

import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import type {RunEvent} from '@vibesys/backend-client';

/**
 * One recorded journal line.
 *
 * Deliberately weaker than the generated `RunEvent`: a legacy capture omits
 * fields today's model defaults in, and the harness validates nothing at
 * runtime. `tests/server/test_tui_dev_harness.py` is what holds every fixture
 * line to the real model.
 */
export interface RunEventRecord {
  sequence?: number;
  timestamp?: string;
  type?: string;
  run_id?: string;
  status?: string | null;
  round_label?: string | null;
  agent_kind?: string | null;
  invocation_id?: string | null;
  execution_id?: string | null;
  data?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/**
 * The payload the translation builds for a rewritten `invocation_started`.
 *
 * Taken from the generated protocol types, so a field added to or renamed in
 * `AgentExecutionStartedData` is a typecheck error here rather than a payload
 * the client silently reads nothing out of.
 */
type AgentExecutionStartedData = Extract<
  NonNullable<RunEvent['data']>,
  {kind?: 'agent_execution_started'}
>;

/** Lifecycle event types in the spelling the client folds. */
const CANONICAL_LIFECYCLE_TYPES = new Set(['agent_execution_started', 'agent_execution_finished']);

/** The same two boundaries under the names they were recorded with earlier. */
const LEGACY_LIFECYCLE_TYPES = new Set(['invocation_started', 'invocation_finished']);

export function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback;
}

export function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/**
 * Reads a journal by path, plain or gzipped, without canonicalizing it.
 *
 * Recorded streams are already ordered and numbered, but a hand-edited fixture
 * may not be, and both the client and the translation below fold strictly by
 * sequence.
 */
export function readJournalRecords(path: string): RunEventRecord[] {
  const raw = readFileSync(path);
  const text = path.endsWith('.gz') ? gunzipSync(raw).toString('utf8') : raw.toString('utf8');
  const records: RunEventRecord[] = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    records.push(JSON.parse(line) as RunEventRecord);
  }
  if (records.length === 0) throw new Error(`journal ${path} contains no events`);
  return records.map((record, index) => ({...record, sequence: record.sequence ?? index + 1}));
}

/** Mirrors `RunEvent._execution_identity_compatibility`. */
function withExecutionIdentity(event: RunEventRecord): RunEventRecord {
  const executionId = event.execution_id ?? null;
  const invocationId = event.invocation_id ?? null;
  if (executionId === null && invocationId !== null) {
    return {...event, execution_id: invocationId};
  }
  if (invocationId === null && executionId !== null) {
    // Kept for the same reason the model keeps it: an older presentation client
    // still correlates streamed output by the legacy name.
    return {...event, invocation_id: executionId};
  }
  return event;
}

/** Execution ids that recorded a lifecycle event of one of `types`. */
function lifecycleExecutionIds(events: RunEventRecord[], types: Set<string>): Set<string> {
  const ids = new Set<string>();
  for (const event of events) {
    const executionId = event.execution_id ?? null;
    if (executionId !== null && event.type !== undefined && types.has(event.type)) {
      ids.add(executionId);
    }
  }
  return ids;
}

/** Mirrors `_attempt_from_label`. */
function attemptFromLabel(roundLabel: string): number | null {
  const digits = /retry-(\d+)/.exec(roundLabel)?.[1];
  return digits === undefined ? null : Number.parseInt(digits, 10);
}

/** Mirrors `_initial_activity_summary`. */
function initialActivitySummary(kind: string): string {
  const normalized = kind.toLowerCase();
  if (normalized.includes('orchestrat') || normalized.includes('plan')) return 'Planning';
  if (normalized.includes('implement')) return 'Implementing';
  if (normalized.includes('judge') || normalized.includes('review')) return 'Reviewing';
  if (normalized.includes('profil') || normalized.includes('benchmark')) return 'Profiling';
  if (normalized === 'chat') return 'Answering question';
  return `Running ${kind}`;
}

/** Python's `x or fallback`, which an empty string also takes. */
function nonEmptyOr(value: string | null | undefined, fallback: string): string {
  return value === null || value === undefined || value === '' ? fallback : value;
}

/**
 * The events a client would receive for `records`.
 *
 * Both stages of the server's read path, in its order: execution identity
 * first, because the lifecycle translation keys off `execution_id`, then the
 * lifecycle translation itself.
 */
export function canonicalJournalEvents(records: RunEventRecord[]): RunEventRecord[] {
  const events = records.map(withExecutionIdentity);
  const canonicalIds = lifecycleExecutionIds(events, CANONICAL_LIFECYCLE_TYPES);
  const canonical: RunEventRecord[] = [];
  for (const event of events) {
    const type = event.type;
    const executionId = event.execution_id ?? null;
    const data = event.data ?? null;
    // A journal recorded across the rename holds both spellings of the same
    // boundary. The canonical one wins and the legacy one is dropped, so the
    // client folds one start and one finish per execution, not two.
    const supersededLegacy =
      type !== undefined &&
      LEGACY_LIFECYCLE_TYPES.has(type) &&
      executionId !== null &&
      canonicalIds.has(executionId);
    if (supersededLegacy) continue;
    if (type === 'invocation_started' && data?.['kind'] === 'invocation_started') {
      const agentKind = nonEmptyOr(event.agent_kind, 'agent');
      const started: AgentExecutionStartedData = {
        kind: 'agent_execution_started',
        stage: agentKind,
        attempt: attemptFromLabel(event.round_label ?? ''),
        // Defaulted rather than read strictly: `AgentExecutionStartedData`
        // defaults both to "", and a record missing them is a record the
        // Python model would have rejected outright, which is what
        // `test_fixture_validates_as_run_events` covers.
        system_prompt: stringOr(data['system_prompt'], ''),
        user_prompt: stringOr(data['user_prompt'], ''),
        // Synthesized, not recovered: the legacy payload carries no activity,
        // and the field is required. The real adapter derives the same opening
        // summary from the agent kind.
        activity: {
          kind: 'agent_execution_activity_changed',
          mode: 'thinking',
          summary: initialActivitySummary(agentKind),
          tool: null,
        },
        // A legacy journal records no provider or model anywhere, and
        // the adapter invents none. The client renders each as absent.
        provider: null,
        model: null,
      };
      canonical.push({...event, type: 'agent_execution_started', data: started});
      continue;
    }
    if (type === 'invocation_finished' && data?.['kind'] === 'invocation_finished') {
      canonical.push({
        ...event,
        type: 'agent_execution_finished',
        // Not typed against the generated `AgentExecutionFinishedData`: its
        // `result` is generated from `Any`, so the schema types it as an
        // object and cannot express the null the server puts on the wire for a
        // finish that carries no result.
        data: {
          kind: 'agent_execution_finished',
          result: data['result'] ?? null,
          error: optionalString(data['error']),
        },
      });
      continue;
    }
    canonical.push(event);
  }
  return canonical;
}
