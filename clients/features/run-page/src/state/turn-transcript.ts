/**
 * The Map's turn-by-turn view of the selected step: what the agents under it said, thought and
 * did, as rows the screen draws in order. The run's transcript (`CoreState.transcript`) joins the
 * trace by the span id the server stamped on each event, never by invocation, name or time: the
 * selected span and the spans under it name the turns, each turn's entries are folded as its own
 * sequence (`transcriptBySpan`), and the turns follow in the order their spans started.
 */
import {roleName, type Span} from '@vibesys/contracts';
import type {CoreState} from '@vibesys/run-state';
import {type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import {type TranscriptEntry, transcriptBySpan} from '@vibesys/run-state/transcript';
import {type ReplyFieldView, replyFields, replyTitle} from './reply-fields.js';
import {duration} from './span-reading.js';
import {ended, plain, statusWords, titleOf} from './span-words.js';

/** What a message is: a reply, the thinking behind it, the prompt it answers, or a note. */
type TurnVoice = 'reply' | 'reasoning' | 'prompt' | 'note';

/** What a command printed and how it ended, as its result reported them in parts. */
interface CommandOutput {
  readonly exitCode: number | null;
  /** How long the command ran, in seconds; null when the result did not say. */
  readonly seconds: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

/** One row of the turn-by-turn view. */
export type TurnRow =
  | {
      readonly kind: 'heading';
      readonly id: string;
      readonly title: string;
      readonly detail: string;
    }
  | {
      readonly kind: 'message';
      readonly id: string;
      readonly author: string;
      readonly variant: TurnVoice;
      readonly tone?: 'ok' | 'err';
      readonly text: string;
      readonly notice?: string;
    }
  | {
      readonly kind: 'reply';
      readonly id: string;
      readonly author: string;
      /** The id of the role that replied (what `author` names for a person); null when unknown. */
      readonly role: string | null;
      /** The reply model's name (`OrchestratorPlan`): what kind of reply this is. */
      readonly replyKind: string;
      readonly title: string;
      readonly fields: readonly ReplyFieldView[];
      /** The validated reply as JSON, for a reader that knows the kind. */
      readonly value: Readonly<Record<string, unknown>>;
      readonly raw: string;
    }
  | {
      readonly kind: 'tool';
      readonly id: string;
      readonly name: string;
      readonly state: 'pending' | 'ok' | 'failed';
      readonly summary?: string;
      readonly meta?: string;
      readonly input: string | null;
      readonly output: string | null;
      /** The result in parts, when it was a command's; `output` is then what the agent was shown. */
      readonly command?: CommandOutput;
      /** Seconds into the turn at which the call was made; absent when the entries carry no time. */
      readonly elapsed?: number;
    };

/** The turns under the selected step: how many there are, and their rows in order. */
export interface TurnTranscript {
  readonly turns: number;
  readonly rows: readonly TurnRow[];
}

const NO_TRANSCRIPT: TurnTranscript = {turns: 0, rows: []};
const SUMMARY_LIMIT = 96;
const TRUNCATED = 'This event was cut at the server’s size bound.';

/** `spanId` and the spans below it, as a set of ids. */
function subtree(spans: readonly Span[], spanId: string): ReadonlySet<string> {
  const ids = new Set([spanId]);
  // Spans come in start order, each after its parent, so one pass finds every descendant.
  for (const span of spans) {
    if (span.parent !== null && ids.has(span.parent)) ids.add(span.id);
  }
  return ids;
}

/** A one-line preview of a tool call: its first text argument. */
function summaryOf(args: Readonly<Record<string, unknown>> | undefined): string | undefined {
  if (args === undefined) return undefined;
  const first = Object.values(args).find(
    (value): value is string => typeof value === 'string' && value.trim() !== '',
  );
  if (first === undefined) return undefined;
  const line = first.split('\n', 1)[0] ?? '';
  return line.length > SUMMARY_LIMIT ? `${line.slice(0, SUMMARY_LIMIT - 1)}…` : line;
}

/** A command's run time: tenths of a second below ten seconds, then as the screen writes any duration. */
function ranFor(seconds: number): string {
  const tenths = Math.round(Math.max(0, seconds) * 10) / 10;
  return tenths < 10 ? `${tenths.toFixed(1)}s` : duration(Math.round(seconds));
}

/** The result of a command as its parts, or undefined for any other result. */
function commandOf(entry: TranscriptEntry): CommandOutput | undefined {
  const payload = entry.toolResult?.payload;
  if (payload?.kind !== 'command') return undefined;
  return {
    exitCode: payload.exit_code ?? null,
    seconds: payload.duration ?? null,
    stdout: payload.stdout,
    stderr: payload.stderr,
  };
}

/** How a command ended in a word or two: `exit 1 · 2.3s`; undefined when it said neither. */
function endingOf(command: CommandOutput): string | undefined {
  const parts = [
    ...(command.exitCode === null ? [] : [`exit ${command.exitCode}`]),
    ...(command.seconds === null ? [] : [ranFor(command.seconds)]),
  ];
  return parts.length === 0 ? undefined : parts.join(' · ');
}

/** The size of a tool's result in a word or two, or why it has none. */
function metaOf(
  state: 'pending' | 'ok' | 'failed',
  output: string | null,
  command: CommandOutput | undefined,
): string | undefined {
  if (state === 'pending') return undefined;
  const ending = command === undefined ? undefined : endingOf(command);
  if (ending !== undefined) return ending;
  if (output === null) return 'no result';
  if (output === '') return 'no output';
  const lines = output.split('\n').length;
  const prefix = state === 'failed' ? 'failed · ' : '';
  if (lines > 1) return `${prefix}${lines} lines`;
  const size =
    output.length < 1000 ? `${output.length} chars` : `${(output.length / 1000).toFixed(1)} KB`;
  return `${prefix}${size}`;
}

/** Seconds into the turn at which the entry happened; none when it or the turn has no time. */
function elapsedOf(entry: TranscriptEntry, origin: number | undefined): {elapsed?: number} {
  if (entry.at === undefined || origin === undefined) return {};
  return {elapsed: Math.max(0, (entry.at - origin) / 1000)};
}

/** The tool's name: the typed call's, else the first line of the legacy call text. */
function nameOf(entry: TranscriptEntry): string {
  if (entry.toolName !== undefined) return entry.toolName;
  return (entry.toolCall ?? '').replace(/^\s*→\s*/, '').split('\n', 1)[0] || 'Tool';
}

function toolRow(entry: TranscriptEntry, turnEnded: boolean, origin: number | undefined): TurnRow {
  const args = entry.toolArguments;
  const settled = entry.toolResult !== undefined;
  const failed = entry.tone === 'failure';
  const typed = entry.toolName !== undefined;
  const name = nameOf(entry);
  const output = typed ? (settled ? (entry.toolResult?.content ?? '') : null) : entry.content;
  const summary = summaryOf(args);
  const state = failed ? 'failed' : settled || turnEnded || !typed ? 'ok' : 'pending';
  const command = commandOf(entry);
  const meta = metaOf(state, output, command);
  return {
    kind: 'tool',
    id: entry.id,
    name,
    state,
    ...(summary === undefined ? {} : {summary}),
    ...(meta === undefined ? {} : {meta}),
    input: args === undefined ? null : JSON.stringify(args, null, 2),
    output,
    ...(command === undefined ? {} : {command}),
    ...elapsedOf(entry, origin),
  };
}

function messageRow(entry: TranscriptEntry, variant: TurnVoice, author: string): TurnRow {
  return {
    kind: 'message',
    id: entry.id,
    author,
    variant,
    ...(entry.tone === 'failure' ? {tone: 'err' as const} : {}),
    ...(entry.tone === 'success' ? {tone: 'ok' as const} : {}),
    text: entry.content,
    ...(entry.truncated === true ? {notice: TRUNCATED} : {}),
  };
}

function replyRow(entry: TranscriptEntry, author: string): TurnRow {
  const reply = entry.reply;
  if (reply === undefined) return messageRow(entry, 'note', author);
  const value = reply.value as Readonly<Record<string, unknown>>;
  return {
    kind: 'reply',
    id: entry.id,
    author,
    role: entry.agentKind ?? null,
    replyKind: reply.kind,
    title: replyTitle(reply.kind, entry.agentKind ?? null),
    fields: replyFields(reply.json_schema as Readonly<Record<string, unknown>>, value),
    value,
    raw: JSON.stringify(value, null, 2),
  };
}

function rowOf(entry: TranscriptEntry, turnEnded: boolean, origin: number | undefined): TurnRow {
  const agent = entry.agentKind === undefined ? 'Agent' : roleName(entry.agentKind);
  switch (entry.kind) {
    case 'assistant':
      return messageRow(entry, 'reply', agent);
    case 'analysis':
      return messageRow(entry, 'reasoning', agent);
    case 'prompt':
      return messageRow(entry, 'prompt', agent);
    case 'reply':
      return replyRow(entry, agent);
    case 'tool':
      return toolRow(entry, turnEnded, origin);
    case 'diagnostic':
    case 'subprocess':
    case 'status':
    case 'result':
      return messageRow(entry, 'note', entry.label ?? plain(entry.kind));
  }
}

/**
 * The turns of the step `span`: for it and every span under it that has entries, a heading (the
 * span's title and state) and its entries as rows, in the order the spans started; none when the
 * trace does not hold `span`. Null until the run's fold has reached the window, so the screen can
 * tell that from a step with no turns.
 */
export function turnTranscript(
  trace: RunTrace,
  core: CoreState | null,
  span: string,
): TurnTranscript | null {
  if (core === null) return null;
  const selected = trace.spans.get(span);
  if (selected === undefined) return NO_TRANSCRIPT;
  const spans = traceSpans(trace);
  const entries = transcriptBySpan(core.transcript, subtree(spans, selected.id));
  const rows: TurnRow[] = [];
  let turns = 0;
  for (const span of spans) {
    const own = entries.get(span.id);
    if (own === undefined) continue;
    turns += 1;
    rows.push({
      kind: 'heading',
      id: `turn ${span.id}`,
      title: titleOf(span),
      detail: statusWords(span.status).label,
    });
    // A turn's clock starts at its first timed entry.
    const origin = own.find(entry => entry.at !== undefined)?.at;
    for (const entry of own) rows.push(rowOf(entry, ended(span), origin));
  }
  return {turns, rows};
}
