/**
 * An opened step as the Run screen reads it. An agent turn reads as a narrative: its structured
 * reply first, then the agent's thinking and text in time order, with the tool calls between two
 * paragraphs folded into one group. A gate reads as a verdict, its numbers and its log. Both are
 * read from the rows `turnTranscript` already builds and from the trace span the step names; the
 * view only draws them.
 */
import type {Span} from '@vibesys/contracts';
import type {CoreState} from '@vibesys/run-state';
import {type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import {transcriptBySpan} from '@vibesys/run-state/transcript';
import {attrsOf, duration, formatValue, ratio} from './span-reading.js';
import {plain, statusWords, verdictWords} from './span-words.js';
import {type TurnRow, turnTranscript} from './turn-transcript.js';
import type {Tone} from './views.js';

type ToolRow = Extract<TurnRow, {kind: 'tool'}>;
type ReplyRow = Extract<TurnRow, {kind: 'reply'}>;

/** One block of an agent turn's narrative. */
export type NarrativeBlock =
  | {readonly kind: 'reply'; readonly id: string; readonly reply: ReplyRow}
  | {readonly kind: 'thought'; readonly id: string; readonly text: string}
  | {readonly kind: 'said'; readonly id: string; readonly text: string}
  /** Consecutive tool calls; its id is the first call's. */
  | {readonly kind: 'tools'; readonly id: string; readonly tools: readonly ToolRow[]};

/** An agent turn read as narrative, and what it did in numbers. */
export interface TurnNarrative {
  readonly blocks: readonly NarrativeBlock[];
  readonly tools: number;
  readonly failed: number;
}

type MessageRow = Extract<TurnRow, {kind: 'message'}>;

/** What the agent thought or said in a message; null for a prompt, an ordinary note or no text. */
function heardBlock(row: MessageRow): NarrativeBlock | null {
  const text = row.text.trim();
  if (text === '') return null;
  if (row.variant === 'reasoning') return {kind: 'thought', id: row.id, text};
  if (row.variant === 'reply') return {kind: 'said', id: row.id, text};
  if (row.variant === 'note' && row.tone === 'err') {
    return {kind: 'said', id: row.id, text: `${row.author}: ${text}`};
  }
  return null;
}

/**
 * The narrative of `rows`: replies first, then thinking and text in order, each run of tool calls
 * (with nothing said between them) as one block. Prompts and notes are not the agent's account,
 * except a note that failed, which is said as it is.
 */
export function narrativeOf(rows: readonly TurnRow[]): TurnNarrative {
  const replies: NarrativeBlock[] = [];
  const blocks: NarrativeBlock[] = [];
  let run: ToolRow[] = [];
  const flush = (): void => {
    const first = run[0];
    if (first !== undefined) blocks.push({kind: 'tools', id: first.id, tools: run});
    run = [];
  };
  for (const row of rows) {
    if (row.kind === 'tool') {
      run.push(row);
    } else if (row.kind === 'reply') {
      replies.push({kind: 'reply', id: row.id, reply: row});
    } else if (row.kind === 'message') {
      const heard = heardBlock(row);
      if (heard === null) continue;
      flush();
      blocks.push(heard);
    }
  }
  flush();
  const tools = rows.filter((row): row is ToolRow => row.kind === 'tool');
  return {
    blocks: [...replies, ...blocks],
    tools: tools.length,
    failed: tools.filter(row => row.state === 'failed').length,
  };
}

/** The narrative of the agent turn `span`; null until the run's fold has reached the page. */
export function turnNarrative(
  trace: RunTrace,
  core: CoreState | null,
  span: string,
): TurnNarrative | null {
  const transcript = turnTranscript(trace, core, {filter: 'all', open: new Set(), selected: span});
  return transcript === null ? null : narrativeOf(transcript.rows);
}

/** The tool call `item` in `narrative`, and the block that holds it. */
export function toolOf(
  narrative: TurnNarrative,
  item: string,
): {readonly tool: ToolRow; readonly block: string} | null {
  for (const block of narrative.blocks) {
    if (block.kind !== 'tools') continue;
    const tool = block.tools.find(each => each.id === item);
    if (tool !== undefined) return {tool, block: block.id};
  }
  return null;
}

/** "1m 40s · 14 tools · 1 failed", for a turn that ran for `seconds` (null: unknown). */
export function turnMeta(seconds: number | null, narrative: TurnNarrative): string {
  const parts = [
    ...(seconds === null ? [] : [duration(seconds)]),
    `${narrative.tools} ${narrative.tools === 1 ? 'tool' : 'tools'}`,
    ...(narrative.failed > 0 ? [`${narrative.failed} failed`] : []),
  ];
  return parts.join(' · ');
}

/** A line of a gate's log; `err` lines are drawn in the error color. */
export interface GateLine {
  readonly text: string;
  readonly stream: 'out' | 'err';
}

/** A gate: its verdict, its numbers as `[name, value]` pairs, and its log. */
export interface GateView {
  readonly verdict: {readonly label: string; readonly tone: Tone};
  readonly numbers: readonly (readonly [string, string])[];
  readonly lines: readonly GateLine[];
}

function numberAt(attrs: Readonly<Record<string, unknown>>, key: string): number | null {
  const value = attrs[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** How far a failed check came: `93/100 trials`, or null when it reported no progress. */
function progressWords(failed: Readonly<Record<string, unknown>>): string | null {
  const done = numberAt(failed, 'completed');
  const required = numberAt(failed, 'required');
  if (done === null || required === null) return null;
  const unit = typeof failed['unit'] === 'string' ? ` ${failed['unit']}` : '';
  return `${formatValue(done)}/${formatValue(required)}${unit}`;
}

function numbersOf(span: Span, parentValue: number | null): [string, string][] {
  const attrs = attrsOf(span);
  const numbers: [string, string][] = [];
  const value = numberAt(attrs, 'metric_value');
  if (value !== null) numbers.push(['value', formatValue(value)]);
  const input = numberAt(attrs, 'vs_baseline');
  if (input !== null) numbers.push(['vs input', ratio(input)]);
  if (value !== null && parentValue !== null && parentValue !== 0) {
    numbers.push(['vs parent', ratio(value / parentValue)]);
  }
  const failed = attrs['failed'];
  if (typeof failed === 'object' && failed !== null) {
    const check = (failed as Readonly<Record<string, unknown>>)['check'];
    const progress = progressWords(failed as Readonly<Record<string, unknown>>);
    const name = typeof check === 'string' ? plain(check) : 'A check';
    numbers.push(['failed check', progress === null ? name : `${name} · ${progress}`]);
  }
  return numbers;
}

/** The gate's log: what the run recorded under its span, one line per line of text. */
function logOf(core: CoreState | null, span: string): GateLine[] {
  if (core === null) return [];
  const entries = transcriptBySpan(core.transcript, new Set([span])).get(span) ?? [];
  return entries.flatMap(entry => {
    const stream = entry.tone === 'failure' || entry.label?.endsWith('stderr') ? 'err' : 'out';
    const lines = entry.content === '' ? [] : entry.content.split('\n');
    return [
      ...(entry.command === undefined
        ? []
        : [{text: `$ ${entry.command}`, stream: 'out' as const}]),
      ...lines.map(text => ({text, stream}) as GateLine),
    ];
  });
}

/**
 * The gate `span`: the verdict (the search's, else the span's state), its value and ratios, and
 * its log. `parentValue` is what the candidate it built on measured, for the ratio to it. Null
 * when the span is not in the trace.
 */
export function gateView(
  trace: RunTrace,
  core: CoreState | null,
  span: string,
  parentValue: number | null,
): GateView | null {
  const found = traceSpans(trace).find(each => each.id === span);
  if (found === undefined) return null;
  return {
    verdict: verdictWords(found) ?? statusWords(found.status),
    numbers: numbersOf(found, parentValue),
    lines: logOf(core, span),
  };
}

/** One tool call in full as the item pane reads it, in order: input, then result. */
export function itemLines(tool: ToolRow): GateLine[] {
  const section = (title: string, text: string | null, stream: 'out' | 'err'): GateLine[] =>
    text === null || text === ''
      ? []
      : [{text: title, stream: 'out'}, ...text.split('\n').map(line => ({text: line, stream}))];
  return [
    ...section('Input', tool.input, 'out'),
    ...(tool.input !== null && tool.output !== null && tool.output !== ''
      ? [{text: '', stream: 'out' as const}]
      : []),
    ...section(
      tool.state === 'failed' ? 'Error' : 'Result',
      tool.output,
      tool.state === 'failed' ? 'err' : 'out',
    ),
  ];
}
