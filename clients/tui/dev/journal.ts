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
 * `execution_id` is unset, so `bad-cpp-round1.jsonl`, the default fixture,
 * replayed with no agent executions at all.
 *
 * The fixtures are protocol version 1 journals. Each canonicalized line is then
 * upgraded to the version 2 wire form (`src/server/wire/upgrade.py`), driven by
 * the proto descriptors through protobuf-es reflection, and parsed as a
 * `RunEvent`.
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
import {
  type DescEnum,
  type DescField,
  type DescMessage,
  fromJson,
  type JsonObject,
} from '@bufbuild/protobuf';
import {PROTOCOL_VERSION, type RunEvent, RunEventSchema} from '@vibesys/backend-client';

/**
 * One recorded journal line, in the version 1 shape it was written with.
 *
 * Deliberately weaker than the generated `RunEvent`: a legacy capture omits
 * fields today's model defaults in, and nothing is validated until the
 * upgraded line is parsed. `tests/server/test_tui_dev_harness.py` is what holds
 * every fixture line to the real model.
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

/** Lifecycle event types in the spelling the client folds. */
const CANONICAL_LIFECYCLE_TYPES = new Set(['agent_execution_started', 'agent_execution_finished']);

/** The same two boundaries under the names they were recorded with earlier. */
const LEGACY_LIFECYCLE_TYPES = new Set(['invocation_started', 'invocation_finished']);

function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback;
}

function optionalString(value: unknown): string | null {
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
 * Both stages of the server's read path on version 1 records, in its order:
 * execution identity first, because the lifecycle translation keys off
 * `execution_id`, then the lifecycle translation itself.
 */
function canonicalRecords(records: RunEventRecord[]): RunEventRecord[] {
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
      canonical.push({
        ...event,
        type: 'agent_execution_started',
        data: {
          kind: 'agent_execution_started',
          stage: agentKind,
          attempt: attemptFromLabel(event.round_label ?? ''),
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
          // A legacy journal records no driver, provider, or model anywhere, and
          // the adapter invents none.
          driver: null,
          provider: null,
          model: null,
        },
      });
      continue;
    }
    if (type === 'invocation_finished' && data?.['kind'] === 'invocation_finished') {
      canonical.push({
        ...event,
        type: 'agent_execution_finished',
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

/** Mirrors `upgrade._DATA_FIELDS`: the `data` oneof of `RunEvent`, by payload kind. */
const DATA_FIELDS = new Map<string, DescField>(
  RunEventSchema.oneofs.find(oneof => oneof.name === 'data')?.fields.map(f => [f.name, f]) ?? [],
);

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function without(record: Record<string, unknown>, ...keys: string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(record).filter(([key]) => !keys.includes(key)));
}

/** Mirrors `enums.prefix`: the first value's name minus `UNSPECIFIED`. */
function enumPrefix(desc: DescEnum): string {
  return (desc.values[0]?.name ?? '').replace(/UNSPECIFIED$/, '');
}

function fieldEnum(field: DescField): DescEnum | undefined {
  if (field.fieldKind === 'enum') return field.enum;
  if (field.fieldKind === 'list' && field.listKind === 'enum') return field.enum;
  return undefined;
}

function fieldMessage(field: DescField): DescMessage | undefined {
  if (field.fieldKind === 'message') return field.message;
  if (field.fieldKind === 'list' && field.listKind === 'message') return field.message;
  return undefined;
}

function convertOne(field: DescField, value: unknown): unknown {
  const enumDesc = fieldEnum(field);
  if (enumDesc !== undefined && typeof value === 'string') {
    const name = enumPrefix(enumDesc) + value.toUpperCase().replaceAll('-', '_');
    if (!enumDesc.values.some(candidate => candidate.name === name)) {
      throw new Error(`${JSON.stringify(value)} is not a ${enumDesc.name}`);
    }
    return name;
  }
  const message = fieldMessage(field);
  if (message && !message.typeName.startsWith('google.protobuf.') && isObject(value)) {
    return convert(message, value);
  }
  return value;
}

/** Mirrors `upgrade._convert`: drop nulls and prefix enum strings, recursively. */
function convert(desc: DescMessage, record: Record<string, unknown>): Record<string, unknown> {
  const converted: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (value === null || value === undefined) continue;
    const field = desc.fields.find(candidate => candidate.name === key);
    // A version 1 discriminator constant on a nested payload.
    if (field === undefined && key === 'kind') continue;
    if (field === undefined) converted[key] = value;
    else if (field.fieldKind === 'list' && Array.isArray(value)) {
      converted[key] = value.map(item => convertOne(field, item));
    } else converted[key] = convertOne(field, value);
  }
  return converted;
}

/** Mirrors `upgrade._tool_result_body`: the `payload` union becomes two typed keys. */
function toolResultBody(input: Record<string, unknown>): Record<string, unknown> {
  const {payload, ...body} = input;
  if (payload === null || payload === undefined) return body;
  if (!isObject(payload)) throw new Error('tool result payload is not an object');
  if (payload['kind'] === 'command') return {...body, command: without(payload, 'kind')};
  if (payload['kind'] === 'json') return {...body, json: {value: payload['value'] ?? null}};
  throw new Error(`unknown tool result payload kind ${JSON.stringify(payload['kind'])}`);
}

/**
 * A version 1 record of any message type, upgraded the way `upgradeEvent`
 * upgrades an event: nulls dropped, enum strings prefixed. For data the harness
 * keeps in version 1 form, such as an experiments sidecar.
 */
export function upgradeRecord(desc: DescMessage, record: Record<string, unknown>): JsonObject {
  return convert(desc, record) as JsonObject;
}

/** Mirrors `upgrade.upgrade_event`: the version 2 JSON object for a version 1 record. */
export function upgradeEvent(record: RunEventRecord): JsonObject {
  if (record.protocol_version === PROTOCOL_VERSION) return record as JsonObject;
  const upgraded = convert(RunEventSchema, without(record, 'data', 'invocation_id'));
  upgraded['protocol_version'] = PROTOCOL_VERSION;
  // v1 mirrored the two ids in both directions; v2 keeps the canonical one.
  const executionId = record.execution_id || record.invocation_id;
  if (executionId) upgraded['execution_id'] = executionId;
  const data = record.data;
  if (data !== null && data !== undefined) {
    const kind = data['kind'];
    const field = typeof kind === 'string' ? DATA_FIELDS.get(kind) : undefined;
    if (field === undefined || field.fieldKind !== 'message') {
      throw new Error(`unknown event payload kind ${JSON.stringify(kind)}`);
    }
    const body = without(data, 'kind');
    upgraded[field.name] = convert(
      field.message,
      kind === 'tool_result' ? toolResultBody(body) : body,
    );
  }
  return upgraded as JsonObject;
}

/**
 * The events a client would receive for `records`, as version 2 messages:
 * canonicalized as the server's read path does, then upgraded and parsed.
 */
export function canonicalJournalEvents(records: RunEventRecord[]): RunEvent[] {
  return canonicalRecords(records).map(record => fromJson(RunEventSchema, upgradeEvent(record)));
}
