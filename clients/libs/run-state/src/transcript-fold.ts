/**
 * The transcript model and its fold: the event-to-`TranscriptEntry` projection and the engine that
 * folds entries into a transcript. The run fold calls into this module; nothing here imports back,
 * so the dependency stays one-way.
 */
import {type RunEvent, roleName} from '@vibesys/contracts';
import {ownProjectionInput, type ReadonlyProjection} from './publication.js';
import {type RoundKey, roundKeyFor, roundNumberFor} from './round-key.js';
import {beforeStructuredReply} from './transcript-reply-suffix.js';

type RunEventData = NonNullable<RunEvent['data']>;
type TypedReply = NonNullable<Extract<RunEventData, {kind: 'agent_execution_finished'}>['reply']>;
type TypedToolResult = ReadonlyProjection<Extract<RunEventData, {kind: 'tool_result'}>>;
/** Typed structure a producer preserved alongside the raw tool-result text. */
export type ToolResultPayload = NonNullable<TypedToolResult['payload']>;

/** One entry of a run's or a chat thread's transcript. */
export interface TranscriptEntry {
  readonly id: string;
  readonly kind:
    | 'assistant'
    | 'prompt'
    | 'analysis'
    | 'tool'
    | 'diagnostic'
    | 'subprocess'
    | 'status'
    | 'result'
    | 'reply';
  readonly content: string;
  readonly label?: string;
  readonly tone?: 'normal' | 'success' | 'failure';
  readonly agentKind?: string;
  readonly roundLabel?: string;
  readonly roundNumber?: number;
  readonly roundKey?: RoundKey;
  readonly turnId?: string;
  /**
   * The agent-turn span of the run trace this entry belongs to, as the server stamped it on the
   * event (`RunEvent.span_id`). Entries join the trace by this id and by nothing else; an entry
   * the server did not tie to a span has none.
   */
  readonly spanId?: string;
  readonly invocationId?: string;
  /**
   * When the event this entry records happened, in epoch milliseconds. A text entry glued from
   * chunks keeps its first chunk's time, and a tool call joined by its result keeps the call's.
   * Absent for an entry whose source carries no time, or only the epoch (a time never set).
   */
  readonly at?: number;
  readonly startsTurn?: boolean;
  readonly toolCall?: string;
  /**
   * A shell command to give code treatment instead of word-wrapped prose.
   * Populated straight from a typed `gate_started` event's `command` field,
   * or, for recorded/legacy prose, split out by `splitFrameworkValidationCommand`.
   */
  readonly command?: string;
  readonly toolResponse?: string;
  readonly toolName?: string;
  readonly toolCallId?: string;
  readonly toolArguments?: ReadonlyProjection<Record<string, unknown>>;
  readonly toolResult?: TypedToolResult;
  /**
   * For a `reply` entry, the typed reply a structured turn ended with (`agent_execution_finished`'s
   * `data.reply`): its model's name, JSON Schema and validated value. `content` is the raw text it
   * came from, which this entry replaces among the turn's streamed assistant text.
   */
  readonly reply?: ReadonlyProjection<TypedReply>;
  /** The server cut this event's payload at its recorded-size bound. */
  readonly truncated?: true;
}

export function eventToTranscriptEntry(event: RunEvent): TranscriptEntry | null {
  const entry = projectedEntry(event);
  if (entry === null) return entry;
  const spanId = event.span_id;
  const at = Date.parse(event.timestamp);
  return {
    ...entry,
    ...(spanId === undefined || spanId === null ? {} : {spanId}),
    // The epoch is how a time that was never set reads, so it is no time.
    ...(Number.isFinite(at) && at > 0 ? {at} : {}),
  };
}

function projectedEntry(event: RunEvent): TranscriptEntry | null {
  const fields = transcriptFields(event);
  const dataEntry = eventDataToTranscriptEntry(event, fields);
  const entry = dataEntry === undefined ? eventTypeToTranscriptEntry(event, fields) : dataEntry;
  if (event.truncated !== true) return entry;
  if (entry !== null) return {...entry, truncated: true};
  return {
    id: fields.id,
    kind: 'diagnostic',
    content: event.text ?? '',
    label: labelFor(event, event.type),
    ...fields.agentFields,
    ...fields.roundFields,
    truncated: true,
  };
}

interface TranscriptFields {
  id: string;
  turnId: string;
  agentFields: {agentKind?: string};
  roundFields: {roundLabel?: string; roundNumber?: number; roundKey?: RoundKey};
}

const UNSEQUENCED_TRANSCRIPT_ID_PREFIX = 'unsequenced:';

function transcriptFields(event: RunEvent): TranscriptFields {
  const roundKey = roundKeyFor(event);
  const roundNumber = roundNumberFor(roundKey);
  return {
    id: transcriptEventId(event),
    turnId: transcriptTurnId(event),
    agentFields: event.agent_kind ? {agentKind: event.agent_kind} : {},
    roundFields: {
      ...(event.round_label ? {roundLabel: event.round_label} : {}),
      ...(roundNumber === null ? {} : {roundNumber}),
      ...(roundKey === null ? {} : {roundKey}),
    },
  };
}

/** A stable presentation identity for a sequenced event or one legacy event value. */
function transcriptEventId(event: RunEvent): string {
  const sequence = event.sequence ?? 0;
  if (sequence > 0) return String(sequence);
  return `${UNSEQUENCED_TRANSCRIPT_ID_PREFIX}${semanticFingerprint([
    event.timestamp,
    event.type,
    event.run_id,
    event.text,
    event.diagnostic,
    event.status,
    event.round_label,
    event.agent_kind,
    event.invocation_id,
    event.execution_id,
    event.chat_thread_id,
    event.data,
    event.truncated,
  ])}`;
}

/** Legacy chunks have no sequence identity, so their durable scope identifies their turn. */
function transcriptTurnId(event: RunEvent): string {
  const sequence = event.sequence ?? 0;
  if (sequence > 0) return String(sequence);
  const data = event.data;
  const channel = data?.kind === 'agent_output_chunk' ? data.channel : null;
  return `${UNSEQUENCED_TRANSCRIPT_ID_PREFIX}turn:${JSON.stringify([
    event.type,
    event.execution_id ?? null,
    event.invocation_id ?? null,
    event.chat_thread_id ?? null,
    event.agent_kind ?? null,
    event.round_label ?? null,
    data?.kind ?? null,
    channel,
  ])}`;
}

const FINGERPRINT_SEEDS = [
  0x243f6a88, 0x85a308d3, 0x13198a2e, 0x03707344, 0xa4093822, 0x299f31d0, 0x082efa98, 0xec4e6c89,
] as const;
const FINGERPRINT_PRIMES = [
  0x01000193, 0x85ebca6b, 0xc2b2ae35, 0x27d4eb2f, 0x165667b1, 0x9e3779b1, 0x7feb352d, 0x846ca68b,
] as const;

/** Fixed-size deterministic digest that consumes canonical semantics without retaining payloads. */
class SemanticFingerprint {
  readonly #lanes: number[] = [...FINGERPRINT_SEEDS];

  write(value: string): void {
    for (let at = 0; at < value.length; at += 1) {
      const code = value.charCodeAt(at);
      for (let lane = 0; lane < this.#lanes.length; lane += 1) {
        this.#lanes[lane] = Math.imul(
          (this.#lanes[lane] as number) ^ code,
          FINGERPRINT_PRIMES[lane] as number,
        );
      }
    }
  }

  finish(): string {
    return this.#lanes.map(value => (value >>> 0).toString(16).padStart(8, '0')).join('');
  }
}

/** Canonically fingerprints JSON-like protocol data, including cyclic caller-owned arguments. */
function semanticFingerprint(value: unknown): string {
  const digest = new SemanticFingerprint();
  appendSemanticValue(digest, value, new Map<object, number>());
  return digest.finish();
}

function appendSemanticValue(
  digest: SemanticFingerprint,
  value: unknown,
  seen: Map<object, number>,
): void {
  if (value === null || value === undefined) {
    digest.write(`${String(value)};`);
    return;
  }
  if (typeof value !== 'object') {
    const scalar = typeof value === 'symbol' ? String(value.description) : String(value);
    appendScalar(digest, typeof value, scalar);
    return;
  }
  const previous = seen.get(value);
  if (previous !== undefined) {
    appendScalar(digest, 'reference', String(previous));
    return;
  }
  seen.set(value, seen.size);
  if (Array.isArray(value)) {
    appendScalar(digest, 'array-length', String(value.length));
    for (const item of value) appendSemanticValue(digest, item, seen);
    return;
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  appendScalar(digest, 'object-length', String(keys.length));
  for (const key of keys) {
    appendScalar(digest, 'key', key);
    appendSemanticValue(digest, record[key], seen);
  }
}

function appendScalar(digest: SemanticFingerprint, tag: string, value: string): void {
  digest.write(`${tag}:${value.length}:`);
  digest.write(value);
  digest.write(';');
}

/** Projects typed payloads in wire order, preserving mixed-envelope precedence. */
function eventDataToTranscriptEntry(
  event: RunEvent,
  fields: TranscriptFields,
): TranscriptEntry | null | undefined {
  const data = event.data;
  if (data?.kind === 'configuration_failed') {
    return {
      id: fields.id,
      kind: 'result',
      content: configurationFailureContent(data),
      label: 'Configuration failed',
      tone: 'failure',
    };
  }
  if (data?.kind === 'chat') {
    return chatTranscriptEntry(data, fields);
  }
  if (data?.kind === 'agent_output_chunk') {
    return outputTranscriptEntry(event, data, fields);
  }
  if (data?.kind === 'tool_call' || data?.kind === 'tool_result') {
    return toolTranscriptEntry(event, data, fields);
  }
  if (data?.kind === 'agent_execution_finished') {
    return data.reply == null ? undefined : replyTranscriptEntry(event, data, data.reply, fields);
  }
  if (data?.kind === 'subprocess_output') {
    return {
      id: fields.id,
      kind: 'subprocess',
      content: data.content,
      label: `${data.process_kind} · ${data.stream}`,
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  // The warning rides the envelope's `diagnostic`, which `applyDiagnosticEvent`
  // has already folded into `diagnostics`; it is not transcript prose.
  if (data?.kind === 'framework_warning') return null;
  // Likewise the failed input measurement: the server attaches its diagnostic, and the run's state
  // keeps it as `inputFailure`.
  if (data?.kind === 'input_measurement_failed') return null;
  if (
    data?.kind === 'judge_result' ||
    data?.kind === 'benchmark_result' ||
    data?.kind === 'round_finished'
  ) {
    return resultTranscriptEntry(event, data, fields);
  }
  if (
    data?.kind === 'gate_started' ||
    data?.kind === 'gate_finished' ||
    data?.kind === 'workspace_snapshot' ||
    data?.kind === 'run_configured'
  ) {
    return frameworkTranscriptEntry(event, data, fields);
  }
  return undefined;
}

function eventTypeToTranscriptEntry(
  event: RunEvent,
  fields: TranscriptFields,
): TranscriptEntry | null {
  const data = event.data;
  if (event.type === 'phase_started') {
    return {
      id: fields.id,
      kind: 'status',
      content: 'started',
      label: labelFor(event, 'phase'),
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  if (event.type === 'run_failed' || event.type === 'run_interrupted') {
    const interrupted = event.type === 'run_interrupted';
    const interruption =
      data?.kind === 'run_interrupted'
        ? `${data.reason}${data.signal === null ? '' : ` (${data.signal})`}`
        : '';
    return {
      id: fields.id,
      kind: 'result',
      content: event.text || interruption || (interrupted ? 'Run interrupted.' : 'Run failed.'),
      label: interrupted ? 'Run interrupted' : 'Run failed',
      tone: 'failure',
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  return null;
}

function chatTranscriptEntry(
  data: Extract<RunEventData, {kind: 'chat'}>,
  fields: TranscriptFields,
): TranscriptEntry {
  // The invocation id names the turn this answer closes. Records written before
  // the field existed carry none.
  const invocationId = data.invocation_id ?? undefined;
  return {
    id: fields.id,
    kind: 'assistant',
    content: data.answer,
    label: 'Answer',
    ...fields.agentFields,
    ...fields.roundFields,
    ...(invocationId === undefined ? {} : {invocationId}),
  };
}

function outputTranscriptEntry(
  event: RunEvent,
  data: Extract<RunEventData, {kind: 'agent_output_chunk'}>,
  fields: TranscriptFields,
): TranscriptEntry {
  const kind = outputKind(data.channel);
  const invocationId = event.invocation_id ?? undefined;
  const gate = kind === 'diagnostic' ? splitFrameworkValidationCommand(data.content) : null;
  return {
    id: fields.id,
    kind,
    content: gate?.content ?? data.content,
    label: labelFor(event, data.channel),
    ...fields.agentFields,
    ...fields.roundFields,
    turnId: invocationId ?? fields.turnId,
    ...(invocationId === undefined ? {} : {invocationId}),
    ...(kind === 'tool' && data.content.trimStart().startsWith('→ ')
      ? {startsTurn: true, toolCall: data.content}
      : {}),
    ...(gate?.command === undefined ? {} : {command: gate.command}),
  };
}

/** The typed reply of a finished structured turn; `content` keeps the raw text it came from. */
function replyTranscriptEntry(
  event: RunEvent,
  data: Extract<RunEventData, {kind: 'agent_execution_finished'}>,
  reply: TypedReply,
  fields: TranscriptFields,
): TranscriptEntry {
  const invocationId = event.invocation_id ?? event.execution_id ?? undefined;
  return {
    id: fields.id,
    kind: 'reply',
    content: typeof data.result === 'string' ? data.result : JSON.stringify(reply.value),
    label: labelFor(event, 'reply'),
    ...fields.agentFields,
    ...fields.roundFields,
    turnId: invocationId ?? fields.turnId,
    ...(invocationId === undefined ? {} : {invocationId}),
    reply: ownProjectionInput(reply),
  };
}

function toolTranscriptEntry(
  event: RunEvent,
  data: Extract<RunEventData, {kind: 'tool_call' | 'tool_result'}>,
  fields: TranscriptFields,
): TranscriptEntry {
  const invocationId = event.invocation_id ?? undefined;
  if (data.kind === 'tool_call') {
    return {
      id: fields.id,
      kind: 'tool',
      content: '',
      label: labelFor(event, 'tool'),
      ...fields.agentFields,
      ...fields.roundFields,
      turnId: invocationId ?? fields.turnId,
      ...(invocationId === undefined ? {} : {invocationId}),
      startsTurn: true,
      toolName: data.tool,
      toolArguments: ownProjectionInput(data.args ?? {}),
      ...(data.call_id == null ? {} : {toolCallId: data.call_id}),
    };
  }
  return {
    id: fields.id,
    kind: 'tool',
    content: data.content,
    label: labelFor(event, 'tool'),
    ...(data.is_error ? {tone: 'failure' as const} : {}),
    ...fields.agentFields,
    ...fields.roundFields,
    turnId: invocationId ?? fields.turnId,
    toolName: data.tool,
    toolResult: ownProjectionInput(data),
    ...(data.call_id == null ? {} : {toolCallId: data.call_id}),
    ...(invocationId === undefined ? {} : {invocationId}),
  };
}

/** The role whose verdict a `judge_result` event reports; its name is the catalog's. */
const JUDGE_ROLE = 'judge';

function resultTranscriptEntry(
  event: RunEvent,
  data: Extract<RunEventData, {kind: 'judge_result' | 'benchmark_result' | 'round_finished'}>,
  fields: TranscriptFields,
): TranscriptEntry {
  if (data.kind === 'judge_result') {
    return {
      id: fields.id,
      kind: 'result',
      content: data.feedback || `${roleName(JUDGE_ROLE)} returned ${data.verdict}.`,
      label: `${roleName(JUDGE_ROLE)} · ${data.verdict.toUpperCase()}`,
      tone: data.verdict === 'pass' ? 'success' : 'failure',
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  if (data.kind === 'benchmark_result') {
    return {
      id: fields.id,
      kind: 'result',
      content: `${data.metric}: ${data.value} ${data.unit}`,
      label: 'Benchmark',
      tone: 'success',
      ...fields.agentFields,
      ...fields.roundFields,
    };
  }
  const tone =
    data.judge_verdict === 'pass'
      ? 'success'
      : data.judge_verdict === 'fail'
        ? 'failure'
        : 'normal';
  return {
    id: fields.id,
    kind: 'result',
    content: `${data.attempts} attempt(s)`,
    label: `${event.round_label ?? 'Round'} · ${data.judge_verdict.toUpperCase()}`,
    tone,
    ...fields.agentFields,
    ...fields.roundFields,
  };
}

function frameworkTranscriptEntry(
  event: RunEvent,
  data: Extract<
    RunEventData,
    {kind: 'gate_started' | 'gate_finished' | 'workspace_snapshot' | 'run_configured'}
  >,
  fields: TranscriptFields,
): TranscriptEntry {
  if (data.kind === 'gate_started') {
    const recipe = data.recipe == null ? '' : ` ${data.recipe}`;
    return {
      id: fields.id,
      kind: 'status',
      content: `running${recipe}`,
      label: frameworkLabel(`framework-${data.gate}`, event),
      ...fields.roundFields,
      ...(data.command == null ? {} : {command: data.command}),
    };
  }
  if (data.kind === 'gate_finished') {
    return gateFinishedEntry(event, data, fields.id, fields.roundFields);
  }
  if (data.kind === 'workspace_snapshot') {
    return {
      id: fields.id,
      kind: 'status',
      content: workspaceSnapshotContent(data),
      label: frameworkLabel(frameworkSourceName(data.source, null), event),
      ...fields.roundFields,
    };
  }
  return {
    id: fields.id,
    kind: 'status',
    content: runConfiguredContent(data),
    label: frameworkLabel(frameworkSourceName(data.source, null), event),
    ...fields.roundFields,
  };
}

export function configurationFailureContent(data: {
  message: string;
  usage?: string | null;
  code: string;
  stage: string;
}): string {
  const sections = [data.message];
  if (data.usage) sections.push(data.usage);
  sections.push(`Code: ${data.code} · Stage: ${data.stage}`);
  return sections.join('\n\n');
}

/**
 * Legacy/recorded-prose adapter: a live backend on `main` now emits a typed
 * `gate_started` event whose `command` field `eventToTranscriptEntry` reads
 * directly (see above), but a run recorded before #697 (replayed as `agent_output_chunk`/diagnostic)
 * still carries the gate command as free text: loop.py's old `ctx.lprint(f"[framework-validation] running
 * {recipe.name}: {recipe.command}")`. This is the one place that text is
 * folded into an entry, so it is split here rather than let a renderer
 * word-wrap a shell command as prose.
 *
 * Deliberately narrow: only the exact "[framework-validation] running
 * <recipe>: " prefix qualifies, so ordinary diagnostic prose (a colon, the
 * word "running", a bracket tag with a different shape, such as the sibling
 * `[framework-validation] PASS` / `reused PASS: ...` lines) is never mistaken
 * for a command.
 */
const FRAMEWORK_VALIDATION_RUN = /^\[framework-validation\] running [^:\n]+: ([\s\S]*)$/;

function splitFrameworkValidationCommand(content: string): {content: string; command?: string} {
  const match = FRAMEWORK_VALIDATION_RUN.exec(content);
  if (match === null) return {content};
  const raw = match[1] ?? '';
  const command = raw.endsWith('\n') ? raw.slice(0, -1) : raw;
  if (command === '') return {content};
  return {content: content.slice(0, content.length - raw.length), command};
}

function outputKind(channel: string): TranscriptEntry['kind'] {
  if (channel === 'assistant') return 'assistant';
  if (channel === 'prompt') return 'prompt';
  if (channel === 'analysis') return 'analysis';
  if (channel === 'tool') return 'tool';
  return 'diagnostic';
}

function labelFor(event: RunEvent, fallback: string): string {
  const phase = event.agent_kind ?? fallback;
  return event.round_label ? `${phase} · ${event.round_label}` : phase;
}

type GateFinishedData = Extract<RunEventData, {kind: 'gate_finished'}>;
type WorkspaceSnapshotData = Extract<RunEventData, {kind: 'workspace_snapshot'}>;
type RunConfiguredData = Extract<RunEventData, {kind: 'run_configured'}>;
/** The generated closed set of framework subsystems, never a local copy. */
type FrameworkSource = NonNullable<GateFinishedData['source']>;

type RoundFields = Partial<Pick<TranscriptEntry, 'roundLabel' | 'roundNumber'>>;

/**
 * The transcript name of a framework subsystem. Exhaustive over the protocol's
 * closed source set, so a new subsystem is a compile error, with `source_label`
 * as the escape hatch the `other` member carries.
 */
function frameworkSourceName(
  source: FrameworkSource | undefined,
  sourceLabel: string | null | undefined,
): string {
  switch (source) {
    case 'git_tracking':
      return 'git-tracking';
    case 'gpu':
      return 'gpu';
    case 'skypilot':
      return 'skypilot';
    case 'other':
      return sourceLabel ?? 'framework';
    case 'gates':
    case 'loop':
    case undefined:
      return 'framework';
    default: {
      const unhandled: never = source;
      return unhandled;
    }
  }
}

/** `labelFor`'s round suffix without its agent fallback: framework, not agent. */
function frameworkLabel(base: string, event: RunEvent): string {
  return event.round_label ? `${base} · ${event.round_label}` : base;
}

/**
 * A gate outcome; the envelope's `status` carries pass or fail. A completed
 * benchmark measurement keeps rendering as the Benchmark result card
 * `benchmark_result` produced, so the card survives that event's retirement.
 */
function gateFinishedEntry(
  event: RunEvent,
  data: GateFinishedData,
  id: string,
  roundFields: RoundFields,
): TranscriptEntry {
  const label = frameworkLabel(`framework-${data.gate}`, event);
  if (event.status === 'failed') {
    const heading = data.recipe == null ? 'FAIL' : `FAIL: ${data.recipe}`;
    return {
      id,
      kind: 'diagnostic',
      content: data.output_tail ? `${heading}\n${data.output_tail}` : heading,
      label,
      tone: 'failure',
      ...roundFields,
    };
  }
  const measurement =
    data.metric != null && data.value != null
      ? `${data.metric}: ${data.value} ${data.unit ?? data.metric}`
      : null;
  if (data.gate === 'benchmark' && measurement !== null && data.reused !== true) {
    return {
      id,
      kind: 'result',
      content: measurement,
      label: 'Benchmark',
      tone: 'success',
      ...roundFields,
    };
  }
  const passed = data.reused === true ? 'reused PASS' : 'PASS';
  const detail = data.recipe ?? measurement;
  return {
    id,
    kind: 'status',
    content: detail === null ? passed : `${passed}: ${detail}`,
    label,
    tone: 'success',
    ...roundFields,
  };
}

/** Exactly one aspect is populated per event; see `WorkspaceSnapshotData`. */
function workspaceSnapshotContent(data: WorkspaceSnapshotData): string {
  if (data.baseline != null) return `trusted input baseline: ${shortCommit(data.baseline)}`;
  const excluded = data.excluded_paths ?? [];
  if (excluded.length > 0) {
    return `excluded ${excluded.length} path${excluded.length === 1 ? '' : 's'} from snapshots`;
  }
  if (data.commit == null) return `no changes to commit for '${data.label ?? ''}'`;
  return `snapshot '${data.label ?? ''}' at ${shortCommit(data.commit)}`;
}

function shortCommit(commit: string): string {
  return commit.slice(0, 7);
}

function runConfiguredContent(data: RunConfiguredData): string {
  const lines: string[] = [];
  if (data.objective) lines.push(`objective: ${data.objective}`);
  if (data.model) lines.push(`model: ${data.model}`);
  if (data.search_policy) lines.push(`search policy: ${data.search_policy}`);
  if (data.pareto_objectives) lines.push(`pareto objectives: ${data.pareto_objectives}`);
  if (data.benchmark_contract) {
    lines.push('benchmark result contract declared; it owns candidate fitness');
  }
  return lines.length > 0 ? lines.join('\n') : 'run configured';
}

/**
 * Appends one entry to a copy of `previous`, leaving `previous` untouched.
 *
 * Used by the single-event path, where the caller owns an immutable array. A
 * batch folds through `TranscriptBuffer` instead, which applies the same step
 * to one working array.
 */
export function appendTranscript(
  previous: readonly TranscriptEntry[],
  incoming: TranscriptEntry,
): TranscriptEntry[] {
  const next = [...previous];
  foldTranscriptEntry(next, incoming, null);
  return next;
}

/**
 * The transcript fold step, applied in place to `entries`.
 *
 * `index` accelerates the open-tool-call lookup; passing null falls back to
 * scanning, which is what the single-event path does. Both must agree, so the
 * index reproduces `findToolCall`'s search order exactly.
 */
export function foldTranscriptEntry(
  entries: TranscriptEntry[],
  incoming: TranscriptEntry,
  index: TranscriptIndex | null,
): void {
  if (incoming.kind === 'reply') {
    foldReply(entries, incoming, index);
    return;
  }
  if (incoming.kind === 'tool' && !incoming.startsTurn && incoming.toolName !== undefined) {
    const target =
      index === null ? findToolCall(entries, incoming) : index.match(entries, incoming);
    const call = entries[target];
    if (call !== undefined) {
      entries[target] = mergeToolResult(call, incoming);
      return;
    }
  }
  const last = entries.at(-1);
  if (
    last?.kind === 'tool' &&
    incoming.kind === 'tool' &&
    last.invocationId === incoming.invocationId &&
    !incoming.startsTurn &&
    // Gluing onto the last tool entry is the legacy tool-chunk rule, where a
    // response chunk has no way to name its call. A typed result names one, so
    // if the search above found nothing the call is outside the replay window
    // and the result stands alone. Without this, two results whose calls both
    // predate the window would collapse into a single entry.
    incoming.toolCallId === undefined
  ) {
    entries[entries.length - 1] = mergeToolResult(last, incoming);
    return;
  }
  if (
    last !== undefined &&
    last.kind === incoming.kind &&
    // Chunks of one turn share its id; an entry without a turn (a terminal
    // chat answer, or one already closed by `foldChatAnswer`) is complete and
    // must not glue onto a neighbor that is just as complete.
    last.turnId !== undefined &&
    last.turnId === incoming.turnId &&
    (incoming.kind === 'assistant' ||
      incoming.kind === 'prompt' ||
      incoming.kind === 'analysis' ||
      incoming.kind === 'diagnostic')
  ) {
    entries[entries.length - 1] = {
      ...last,
      content: last.content + glue(last, incoming),
      ...(incoming.truncated === true ? {truncated: true as const} : {}),
    };
    return;
  }
  const appended = uniqueTranscriptEntry(entries, incoming, index);
  entries.push(appended);
  index?.record(appended, entries.length - 1);
  capTranscript(entries, index);
}

/** Give a distinct retained card a unique renderer identity without changing its turn identity. */
function uniqueTranscriptEntry(
  entries: readonly TranscriptEntry[],
  incoming: TranscriptEntry,
  index: TranscriptIndex | null,
): TranscriptEntry {
  // A positive sequence already is a unique durable identity, and the run reducer rejects a
  // repeated or stale sequence before it reaches this fold. Keep the ordinary streaming path O(1).
  if (!incoming.id.startsWith(UNSEQUENCED_TRANSCRIPT_ID_PREFIX)) return incoming;
  const contains = (id: string): boolean =>
    index === null ? entries.some(entry => entry.id === id) : index.hasId(id);
  if (!contains(incoming.id)) return incoming;
  let occurrence = 2;
  while (contains(`${incoming.id}#${occurrence}`)) occurrence += 1;
  return {...incoming, id: `${incoming.id}#${occurrence}`};
}

/**
 * What joins `incoming` onto the entry it glues into.
 *
 * Assistant, prompt, and analysis chunks are mid-sentence fragments of a token
 * stream and must concatenate raw; inserting anything between them would break
 * words. Diagnostic chunks are whole lines a driver already terminated in
 * meaning but not in text (`[codex turn started]` carries no newline), so
 * concatenating them raw produced one squished blob per turn.
 */
function glue(last: TranscriptEntry, incoming: TranscriptEntry): string {
  const separator =
    incoming.kind === 'diagnostic' && last.content !== '' && !last.content.endsWith('\n')
      ? '\n'
      : '';
  return separator + incoming.content;
}

const MAX_TRANSCRIPT_ENTRIES = 20_000;

/** Evicts the oldest round in place once the transcript passes its cap. */
function capTranscript(entries: TranscriptEntry[], index: TranscriptIndex | null): void {
  if (entries.length <= MAX_TRANSCRIPT_ENTRIES) return;
  const oldestRound = entries.find(entry => entry.roundNumber !== undefined)?.roundNumber;
  const kept =
    oldestRound === undefined
      ? entries.length
      : retainInPlace(
          entries,
          entry => entry.roundNumber === undefined || entry.roundNumber > oldestRound,
        );
  if (kept === entries.length) entries.splice(0, entries.length - MAX_TRANSCRIPT_ENTRIES);
  else entries.length = kept;
  index?.reindex(entries);
}

/** Compacts the kept entries to the front and returns how many survived. */
function retainInPlace(
  entries: TranscriptEntry[],
  keep: (entry: TranscriptEntry) => boolean,
): number {
  let write = 0;
  for (const entry of entries) {
    if (!keep(entry)) continue;
    entries[write] = entry;
    write += 1;
  }
  return write;
}

function findToolCall(previous: readonly TranscriptEntry[], result: TranscriptEntry): number {
  const indices = Array.from(previous.keys());
  if (result.toolCallId !== undefined) indices.reverse();
  for (const index of indices) {
    const candidate = previous[index];
    if (
      candidate?.kind !== 'tool' ||
      (candidate.toolCall === undefined && candidate.toolArguments === undefined) ||
      candidate.toolResponse !== undefined ||
      candidate.toolResult !== undefined ||
      candidate.invocationId !== result.invocationId
    ) {
      continue;
    }
    if (result.toolCallId !== undefined) {
      if (candidate.toolCallId === result.toolCallId) return index;
    } else if (candidate.toolName === result.toolName) return index;
  }
  return -1;
}

/** A tool call still waiting for its result: what `findToolCall` accepts. */
function isOpenToolCall(entry: TranscriptEntry): boolean {
  return (
    entry.kind === 'tool' &&
    (entry.toolCall !== undefined || entry.toolArguments !== undefined) &&
    entry.toolResponse === undefined &&
    entry.toolResult === undefined
  );
}

// NUL appears in no invocation id, tool name, call id, or thread id, so it
// separates the halves of a key without any real value colliding.
const TOOL_KEY_SEPARATOR = '\u0000';

function toolKey(invocationId: string | undefined, discriminator: string): string {
  return `${invocationId ?? ''}${TOOL_KEY_SEPARATOR}${discriminator}`;
}

/**
 * Indexes retained entry identities and locates tool calls without scanning the transcript.
 *
 * `findToolCall` scans by call id from the end (latest open call wins) and by
 * tool name from the start (earliest open call wins), so this keeps one bucket
 * per key holding candidate positions in ascending order and reads the matching
 * end. Positions of calls that have since been answered are dropped lazily: a
 * call never reopens, so a stale position can only ever be discarded.
 */
class TranscriptIndex {
  readonly #ids = new Set<string>();
  readonly #byCallId = new Map<string, number[]>();
  readonly #byName = new Map<string, number[]>();

  hasId(id: string): boolean {
    return this.#ids.has(id);
  }

  /** Records `entry` at position `at`, including its open-call keys when it has them. */
  record(entry: TranscriptEntry, at: number): void {
    this.#ids.add(entry.id);
    if (!isOpenToolCall(entry)) return;
    if (entry.toolCallId !== undefined) {
      bucket(this.#byCallId, toolKey(entry.invocationId, entry.toolCallId)).push(at);
    }
    if (entry.toolName !== undefined) {
      bucket(this.#byName, toolKey(entry.invocationId, entry.toolName)).push(at);
    }
  }

  /** Rebuilds every bucket after positions shift, i.e. after cap eviction. */
  reindex(entries: readonly TranscriptEntry[]): void {
    this.#ids.clear();
    this.#byCallId.clear();
    this.#byName.clear();
    for (let at = 0; at < entries.length; at += 1) {
      const entry = entries[at];
      if (entry !== undefined) this.record(entry, at);
    }
  }

  /** The position `findToolCall` would return for `result`, or -1. */
  match(entries: readonly TranscriptEntry[], result: TranscriptEntry): number {
    if (result.toolCallId !== undefined) {
      const key = toolKey(result.invocationId, result.toolCallId);
      return this.#take(this.#byCallId, key, entries, 'last');
    }
    if (result.toolName === undefined) return -1;
    return this.#take(
      this.#byName,
      toolKey(result.invocationId, result.toolName),
      entries,
      'first',
    );
  }

  #take(
    buckets: Map<string, number[]>,
    key: string,
    entries: readonly TranscriptEntry[],
    end: 'first' | 'last',
  ): number {
    const positions = buckets.get(key);
    if (positions === undefined) return -1;
    while (positions.length > 0) {
      const at = (end === 'last' ? positions.at(-1) : positions[0]) as number;
      const candidate = entries[at];
      if (candidate !== undefined && isOpenToolCall(candidate)) return at;
      if (end === 'last') positions.pop();
      else positions.shift();
    }
    buckets.delete(key);
    return -1;
  }
}

function bucket(buckets: Map<string, number[]>, key: string): number[] {
  const existing = buckets.get(key);
  if (existing !== undefined) return existing;
  const created: number[] = [];
  buckets.set(key, created);
  return created;
}

/**
 * One transcript folded in place across a batch.
 *
 * The array is mutable only while the batch is folding; `entries` is handed to
 * the committed `CoreState` once, after which nothing writes to it again.
 */
export class TranscriptBuffer {
  readonly entries: TranscriptEntry[];
  readonly #index = new TranscriptIndex();

  constructor(initial: readonly TranscriptEntry[]) {
    this.entries = [...initial];
    this.#index.reindex(this.entries);
  }

  append(incoming: TranscriptEntry): void {
    foldTranscriptEntry(this.entries, incoming, this.#index);
  }
}

/** Key for the run transcript, kept out of the chat thread id space. */
export const RUN_TRANSCRIPT = `${TOOL_KEY_SEPARATOR}run`;

/** How far back a reply looks for the turn's streamed text; turns that run together interleave. */
const REPLY_SCAN = 500;

function replyPrefix(text: string, reply: TranscriptEntry, raw: string): string | undefined {
  if (text.endsWith(raw)) return text.slice(0, text.length - raw.length).trimEnd();
  return reply.reply ? beforeStructuredReply(text, reply.reply.value) : undefined;
}

/**
 * Folds a turn's typed reply over its own streamed text: a structured turn's final assistant text
 * is the reply's raw JSON, so the typed entry takes that text's place (or what is left of the
 * entry once the JSON is cut off its end), and the reply never shows twice, once as braces.
 */
function foldReply(
  entries: TranscriptEntry[],
  reply: TranscriptEntry,
  index: TranscriptIndex | null,
): void {
  const raw = reply.content.trim();
  const turn = reply.invocationId;
  if (raw !== '' && turn !== undefined) {
    const floor = Math.max(0, entries.length - REPLY_SCAN);
    for (let at = entries.length - 1; at >= floor; at -= 1) {
      const entry = entries[at];
      if (entry === undefined || entry.kind !== 'assistant' || entry.invocationId !== turn)
        continue;
      const text = entry.content.trimEnd();
      const rest = replyPrefix(text, reply, raw);
      if (rest === undefined) break;
      if (rest === '') {
        entries[at] = reply;
        index?.record(reply, at);
        return;
      }
      entries[at] = {...entry, content: rest};
      break;
    }
  }
  const appended = uniqueTranscriptEntry(entries, reply, index);
  entries.push(appended);
  index?.record(appended, entries.length - 1);
}

function mergeToolResult(call: TranscriptEntry, result: TranscriptEntry): TranscriptEntry {
  if (call.toolArguments !== undefined && result.toolResult !== undefined) {
    return {
      ...call,
      content: result.toolResult.content,
      toolResult: result.toolResult,
      ...(result.tone === undefined ? {} : {tone: result.tone}),
      ...(result.truncated === true ? {truncated: true as const} : {}),
    };
  }
  const separator = call.content.endsWith('\n') || result.content.startsWith('\n') ? '' : '\n';
  return {
    ...call,
    content: call.content + separator + result.content,
    toolResponse: (call.toolResponse ?? '') + (call.toolResponse ? separator : '') + result.content,
    ...(result.tone === undefined ? {} : {tone: result.tone}),
    ...(result.truncated === true ? {truncated: true as const} : {}),
  };
}
