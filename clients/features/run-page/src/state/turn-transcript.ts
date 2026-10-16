/**
 * The Map's turn-by-turn view of the selected step: what the agents under it said, thought and
 * did, as rows the screen draws in order. The run's transcript (`CoreState.transcript`) joins the
 * trace by the span id the server stamped on each event, never by invocation, name or time: the
 * selected span and the spans under it name the turns, each turn's entries are folded as its own
 * sequence (`transcriptBySpan`), and the turns follow in the order their spans started.
 */
import type {Span} from '@vibesys/contracts';
import type {CoreState} from '@vibesys/run-state';
import {type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import {type TranscriptEntry, transcriptBySpan} from '@vibesys/run-state/transcript';
import {selectedSpan} from './map-view.js';
import type {MapState} from './run-page.js';
import {ended, plain, statusWords, titleOf} from './span-words.js';

/** What a message is: a reply, the thinking behind it, the prompt it answers, or a note. */
export type TurnVoice = 'reply' | 'reasoning' | 'prompt' | 'note';

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
      readonly kind: 'tool';
      readonly id: string;
      readonly name: string;
      readonly state: 'pending' | 'ok' | 'failed';
      readonly summary?: string;
      readonly input: string | null;
      readonly output: string | null;
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

function toolRow(entry: TranscriptEntry, turnEnded: boolean): TurnRow {
  const args = entry.toolArguments;
  const settled = entry.toolResult !== undefined;
  const failed = entry.tone === 'failure';
  const typed = entry.toolName !== undefined;
  const name = typed
    ? (entry.toolName ?? 'Tool')
    : (entry.toolCall ?? '').replace(/^\s*→\s*/, '').split('\n', 1)[0] || 'Tool';
  const output = typed ? (settled ? (entry.toolResult?.content ?? '') : null) : entry.content;
  const summary = summaryOf(args);
  return {
    kind: 'tool',
    id: entry.id,
    name,
    state: failed ? 'failed' : settled || turnEnded || !typed ? 'ok' : 'pending',
    ...(summary === undefined ? {} : {summary}),
    input: args === undefined ? null : JSON.stringify(args, null, 2),
    output,
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

function rowOf(entry: TranscriptEntry, turnEnded: boolean): TurnRow {
  const agent = entry.agentKind === undefined ? 'Agent' : plain(entry.agentKind);
  switch (entry.kind) {
    case 'assistant':
      return messageRow(entry, 'reply', agent);
    case 'analysis':
      return messageRow(entry, 'reasoning', agent);
    case 'prompt':
      return messageRow(entry, 'prompt', agent);
    case 'tool':
      return toolRow(entry, turnEnded);
    case 'diagnostic':
    case 'subprocess':
    case 'status':
    case 'result':
      return messageRow(entry, 'note', entry.label ?? plain(entry.kind));
  }
}

/**
 * The selected step's turns: for the selected span and every span under it that has entries, a
 * heading (the span's title and state) and its entries as rows, in the order the spans started.
 * Null until the run's fold has reached the window, so the screen can tell that from a step with
 * no turns.
 */
export function turnTranscript(
  trace: RunTrace,
  core: CoreState | null,
  map: MapState,
): TurnTranscript | null {
  if (core === null) return null;
  const selected = selectedSpan(trace, map);
  if (selected === null) return NO_TRANSCRIPT;
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
    for (const entry of own) rows.push(rowOf(entry, ended(span)));
  }
  return {turns, rows};
}
