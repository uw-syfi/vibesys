/** A round's transcript as turns, one per agent execution: pure, from store state. */
import type {RunEvent} from '@vibesys/backend-client';
import {
  type AgentPhase,
  type AgentPhaseStatus,
  type CoreState,
  hasRunEnded,
  phasesForRound,
  roundNumberFromLabel,
  type TodoItem,
  type TranscriptEntry,
} from '@vibesys/core-state';
import {
  activityRound,
  formatValue,
  pathShortener,
  prose,
  steers,
  titleCase,
  toolDuration,
} from './derive.js';
import type {DiffLine, ProsePart} from './model.js';
import {planFacts, resultText} from './rounds.js';
import type {SentSteer} from './session.js';

type ToolEnd = {kind: 'time' | 'exit'; text: string} | null;

export interface ToolDescription {
  verb: string;
  object: string | null;
  detail: string | null;
  /** The whole command or path and the agent's description of the call, for the row's hint. */
  hint: string;
  added: number | null;
  removed: number | null;
}

export interface ToolRow extends ToolDescription {
  id: string;
  end: ToolEnd;
  inFlight: boolean;
}

export type TurnItem =
  | {kind: 'prose'; id: string; paragraphs: ProsePart[][]}
  | {kind: 'tools'; id: string; tools: ToolRow[]}
  | {kind: 'steer'; id: string; text: string};

export interface Turn {
  /** The execution's `ExecutionKeys` key: shared with the agent graph and the agent filter. */
  id: string;
  kind: string;
  label: string | null;
  role: string;
  phase: string;
  hint: string;
  active: boolean;
  status: AgentPhaseStatus | null;
  error: string | null;
  prompt: string | null;
  todos: TodoItem[];
  items: TurnItem[];
  verdict: {accepted: boolean; feedback: string} | null;
  working: {lead: string; detail: string | null} | null;
}

export interface QueuedSteer {
  /** The sent steer's client id, else `steer-<sequence>` of its journaled pending event. */
  id: string;
  text: string;
}

export interface RoundTranscript {
  turns: Turn[];
  /** Steers acknowledged or journaled as pending and not consumed yet (live round only). */
  queued: QueuedSteer[];
}

export interface TranscriptInput {
  core: CoreState;
  captured: readonly RunEvent[];
  sent: readonly SentSteer[];
  round: number | null;
  runId: string | null;
}

type Short = (text: string) => string;

/**
 * One key per agent execution, shared by transcript turns, graph nodes and the agent filter.
 * An event or phase with an id is its execution id (an invocation id that names a different
 * execution maps to it through `aliases`). One without ids is a legacy execution: its kind and
 * round label, plus an occurrence number that counts the starts seen so far for that pair, so two
 * attempts under one label stay two executions. Distinct ids are never merged.
 */
class ExecutionKeys {
  readonly #aliases: ReadonlyMap<string, string>;
  readonly #starts = new Map<string, number>();

  constructor(aliases: ReadonlyMap<string, string> = new Map()) {
    this.#aliases = aliases;
  }

  /** `starts` marks the record that opens an execution (a phase or an execution start). */
  key(id: string | null | undefined, kind: string, label: string | null, starts: boolean): string {
    if (id) return this.#aliases.get(id) ?? id;
    const base = `${kind}|${label ?? ''}`;
    const count = (this.#starts.get(base) ?? 0) + (starts ? 1 : 0);
    this.#starts.set(base, count);
    return count <= 1 ? base : `${base}#${count - 1}`;
  }
}

/** Invocation ids that name an execution by another id, from events that carry both. */
function invocationAliases(captured: readonly RunEvent[]): Map<string, string> {
  const aliases = new Map<string, string>();
  for (const event of captured) {
    const {execution_id: execution, invocation_id: invocation} = event;
    if (execution && invocation && execution !== invocation) aliases.set(invocation, execution);
  }
  return aliases;
}

/** Keys for a round's started phases, in start order (pending placeholders have none). */
export function phaseKeys(phases: readonly AgentPhase[]): Map<AgentPhase, string> {
  const keys = new ExecutionKeys();
  const result = new Map<AgentPhase, string>();
  for (const phase of byStart(phases)) {
    if (phase.status === 'pending') continue;
    result.set(
      phase,
      keys.key(phase.executionId ?? phase.invocationId, phase.kind, phase.roundLabel, true),
    );
  }
  return result;
}

export function phaseName(label: string | null, round: number | null): string {
  if (label?.endsWith('-pre')) {
    return round === 1 ? 'Reviewing the starting point' : 'Reviewing the previous round';
  }
  if (label?.endsWith('-plan')) return 'Planning';
  const attempt = /-retry-(\d+)/.exec(label ?? '')?.[1];
  return attempt === undefined ? '' : `Attempt ${attempt}`;
}

/** Phases in the order they started; phases without a start keep their order at the end. */
function byStart(phases: readonly AgentPhase[]): AgentPhase[] {
  const time = (phase: AgentPhase) => {
    const at = Date.parse(phase.startedAt ?? '');
    return Number.isNaN(at) ? Number.POSITIVE_INFINITY : at;
  };
  return phases
    .map((phase, index) => ({phase, index}))
    .sort((left, right) => time(left.phase) - time(right.phase) || left.index - right.index)
    .map(({phase}) => phase);
}

function isLive(core: CoreState, round: number | null): boolean {
  return !hasRunEnded(core) && activityRound(core) === round;
}

function newTurn(
  id: string,
  kind: string,
  label: string | null,
  round: number | null,
  model: string | null,
): Turn {
  return {
    id,
    kind,
    label,
    role: titleCase(kind),
    phase: phaseName(label, round),
    hint: [label, model].filter(Boolean).join(' · '),
    active: false,
    status: null,
    error: null,
    prompt: null,
    todos: [],
    items: [],
    verdict: null,
    working: null,
  };
}

export function roundTranscript(input: TranscriptInput): RoundTranscript {
  const {core, captured, round} = input;
  const turns = new Map<string, Turn>();
  for (const [phase, key] of phaseKeys(phasesForRound(core.phases, round))) {
    turns.set(key, {
      ...newTurn(key, phase.kind, phase.roundLabel, round, phase.model ?? null),
      status: phase.status,
    });
  }
  const records = executionRecords(captured);
  // Turns read in the order they started: the execution start event, else their first entry.
  const first = addEntries(input, turns, invocationAliases(captured));
  const order = (turn: Turn) =>
    Math.min(
      first.get(turn) ?? Number.POSITIVE_INFINITY,
      records.get(turn.id)?.start ?? Number.POSITIVE_INFINITY,
    );
  const list = [...turns.values()]
    .map((turn, index) => ({turn, index}))
    .sort((left, right) => order(left.turn) - order(right.turn) || left.index - right.index)
    .map(({turn}) => turn);
  finishTurns(input, list, records);
  return {turns: list, queued: isLive(core, round) ? queuedSteers(captured, input.sent) : []};
}

/** The entry kinds a turn is built from; a phase start places its turn even before it speaks. */
function turnEntry(entry: TranscriptEntry): boolean {
  if (entry.kind === 'status') return entry.content === 'started';
  return entry.kind === 'tool' || entry.kind === 'assistant' || entry.kind === 'analysis';
}

/** Files each entry of the round under its execution; returns each turn's first sequence. */
function addEntries(
  input: TranscriptInput,
  turns: Map<string, Turn>,
  aliases: ReadonlyMap<string, string>,
): Map<Turn, number> {
  const {core, round} = input;
  const short = pathShortener(input.runId);
  const running = new Set(Object.values(core.activeExecutions).map(run => run.executionId));
  const keys = new ExecutionKeys(aliases);
  const first = new Map<Turn, number>();
  for (const entry of core.transcript) {
    const kind = entry.agentKind;
    if ((entry.roundNumber ?? null) !== round || kind === undefined || !turnEntry(entry)) continue;
    const label = entry.roundLabel ?? null;
    const key = keys.key(entry.invocationId, kind, label, entry.kind === 'status');
    const turn =
      turns.get(key) ??
      (entry.kind === 'status' ? undefined : newTurn(key, kind, label, round, null));
    // A phase start carries no ids here: it opens a legacy occurrence but never creates a turn.
    if (turn === undefined) continue;
    turns.set(key, turn);
    if (!first.has(turn)) first.set(turn, Number(entry.id));
    addItem(turn, entry, short, running);
  }
  return first;
}

function addItem(
  turn: Turn,
  entry: TranscriptEntry,
  short: Short,
  running: ReadonlySet<string>,
): void {
  if (entry.kind === 'tool') {
    addTool(turn, toolRow(entry, short, running));
  } else if (entry.kind !== 'status' && entry.content.trim()) {
    turn.items.push({kind: 'prose', id: entry.id, paragraphs: prose(short(entry.content))});
  }
}

function addTool(turn: Turn, row: ToolRow): void {
  const last = turn.items.at(-1);
  if (last?.kind === 'tools') last.tools.push(row);
  else turn.items.push({kind: 'tools', id: `tools-${row.id}`, tools: [row]});
}

function toolRow(entry: TranscriptEntry, short: Short, running: ReadonlySet<string>): ToolRow {
  const open = entry.toolResult === undefined && entry.toolResponse === undefined;
  return {
    id: entry.id,
    ...describeTool(entry, short),
    end: open ? null : toolEnd(entry),
    inFlight: open && entry.invocationId !== undefined && running.has(entry.invocationId),
  };
}

function toolEnd(entry: TranscriptEntry): ToolEnd {
  const payload = entry.toolResult?.payload;
  const code = payload?.kind === 'command' ? payload.exit_code : null;
  if (typeof code === 'number' && code !== 0) return {kind: 'exit', text: `exit ${code}`};
  if (entry.toolResult?.is_error === true) return {kind: 'exit', text: 'error'};
  const time = toolDuration(entry);
  return time === null ? null : {kind: 'time', text: time};
}

const RG = /^rg\s+(?:-\S+\s+)*(['"])(.+?)\1\s+(\S+)$/;
const SED = /^sed\s+-n\s+(['"])(\d+),(\d+)p\1\s+(\S+)$/;
/** A trailing `2>&1` and `| tail -N` or `| head -N` only trim what the reader sees. */
const TRIM = /\s*(?:2>&1\s*)?(?:\|\s*(?:tail|head)\b.*)?$/s;
const ROOM = 58;

/** Keeps both ends of a long command: the program and the path it ends on. */
function elide(text: string, room = ROOM): string {
  if (text.length <= room) return text;
  return `${text.slice(0, Math.ceil(room * 0.62))}…${text.slice(-Math.floor(room * 0.34))}`;
}

function lineCount(text: unknown): number | null {
  return typeof text === 'string' ? text.replace(/\n$/, '').split('\n').length : null;
}

/** What an edit changed, without the lines its old and new text share at either end. */
function editCounts(before: unknown, after: unknown): Pick<ToolDescription, 'added' | 'removed'> {
  if (typeof before !== 'string' || typeof after !== 'string') {
    return {added: lineCount(after), removed: lineCount(before)};
  }
  const lines = lineDiff(before, after);
  return {
    added: lines.filter(line => line.tone === 'add').length,
    removed: lines.filter(line => line.tone === 'del').length,
  };
}

/** A write's own report of its size (`Wrote src/q.rs (54 lines added)`), else its argument's lines. */
function writtenLines(entry: TranscriptEntry, content: unknown): number | null {
  const reported = /\((\d+) lines? added\)/.exec(
    entry.toolResult?.content ?? entry.toolResponse ?? '',
  )?.[1];
  return reported === undefined ? lineCount(content) : Number(reported);
}

type Describe = (
  verb: string,
  object: string | null,
  extra?: Partial<ToolDescription>,
) => ToolDescription;

function describeCommand(command: string | null, describe: Describe): ToolDescription {
  if (command === null) return describe('Ran', null);
  const shown = command.replace(TRIM, '').trim();
  const search = RG.exec(shown);
  if (search) return describe('Searched', search[3] ?? null, {detail: `for ${search[2]}`});
  const read = SED.exec(shown);
  if (read) return describe('Read', read[4] ?? null, {detail: `lines ${read[2]}–${read[3]}`});
  return describe('Ran', elide(shown));
}

/** One tool call as a row: a verb, what it touched, and line counts for edits. */
export function describeTool(entry: TranscriptEntry, short: Short): ToolDescription {
  const args = entry.toolArguments ?? {};
  const text = (key: string): string | null => {
    const value = args[key];
    return typeof value === 'string' && value !== '' ? short(value) : null;
  };
  const describe: Describe = (verb, object, extra = {}) => ({
    verb,
    object,
    detail: null,
    hint: [text('command') ?? object, text('description')].filter(Boolean).join('\n'),
    added: null,
    removed: null,
    ...extra,
  });
  switch (entry.toolName) {
    case 'Bash':
      return describeCommand(text('command'), describe);
    case 'Edit':
    case 'MultiEdit':
      return describe(
        'Edited',
        text('file_path'),
        editCounts(args['old_string'], args['new_string']),
      );
    case 'Write':
      return describe('Wrote', text('file_path'), {added: writtenLines(entry, args['content'])});
    case 'Read':
      return describe('Read', text('file_path'));
    case 'Grep':
    case 'Glob': {
      const pattern = text('pattern');
      const path = text('path');
      if (path === null) return describe('Searched', pattern);
      return describe('Searched', path, {detail: pattern === null ? null : `for ${pattern}`});
    }
    case 'StructuredOutput':
      return describe('Returned', 'the result');
    case undefined:
      return describe('Tool call', entry.toolCall?.replace(/^\s*→\s*/, '').trim() || null);
    default: {
      const first = Object.values(args).find(value => typeof value === 'string');
      return describe(entry.toolName, typeof first === 'string' ? elide(short(first)) : null);
    }
  }
}

export interface LineStat {
  added: number;
  removed: number;
}

/** Each round's edits: changed lines per file, as the tool calls name the file. */
export type RoundEdits = ReadonlyMap<number, ReadonlyMap<string, LineStat>>;

/**
 * The lines each round's edits and writes changed, per file (the run workspace prefix stripped):
 * the same counts the transcript rows show, summed.
 */
export function roundEdits(core: CoreState, runId: string | null): RoundEdits {
  const short = pathShortener(runId);
  const rounds = new Map<number, Map<string, LineStat>>();
  for (const entry of core.transcript) {
    if (entry.kind !== 'tool' || entry.roundNumber === undefined) continue;
    const {verb, object, added, removed} = describeTool(entry, short);
    if (object === null || (verb !== 'Edited' && verb !== 'Wrote')) continue;
    const stats = rounds.get(entry.roundNumber) ?? new Map<string, LineStat>();
    const stat = stats.get(object) ?? {added: 0, removed: 0};
    stats.set(object, {added: stat.added + (added ?? 0), removed: stat.removed + (removed ?? 0)});
    rounds.set(entry.roundNumber, stats);
  }
  return rounds;
}

/** The stat of `path` (repository-relative) among a round's edits, which may name it absolutely. */
export function statFor(edits: ReadonlyMap<string, LineStat>, path: string): LineStat | null {
  for (const [file, stat] of edits) if (file === path || file.endsWith(`/${path}`)) return stat;
  return null;
}

function finishTurns(
  input: TranscriptInput,
  turns: Turn[],
  records: ReadonlyMap<string, ExecutionRecord>,
): void {
  const {core, captured, round} = input;
  const live = isLive(core, round);
  const passCriteria = round === null ? null : (planFacts(captured, round)?.passCriteria ?? null);
  const acting = new Set(
    [...phaseKeys(phasesForRound(core.phases, round))]
      .filter(([phase]) => phase.status === 'active')
      .map(([, key]) => key),
  );
  // Pause takes effect after the current call, so a paused run has no call in flight.
  const running = live && core.status !== 'paused';
  for (const turn of turns) {
    const record = records.get(turn.id);
    turn.prompt = record?.prompt ?? null;
    turn.error = record?.error ?? null;
    turn.todos = core.todos.find(todos => todos.executionId === turn.id)?.items ?? [];
    const note = record?.note ?? null;
    if (note !== null && !turn.items.some(item => item.kind === 'prose')) {
      turn.items.unshift({kind: 'prose', id: `result-${turn.id}`, paragraphs: prose(note)});
    }
    turn.active = running && acting.has(turn.id);
    turn.working = turn.active
      ? workingOf(turn, passCriteria, round, core.activeExecutions[turn.id]?.activity.summary)
      : null;
  }
  attachVerdicts(captured, round, turns);
  attachSteers(captured, round, turns);
}

function workingOf(
  turn: Turn,
  passCriteria: string | null,
  round: number | null,
  summary?: string,
): Turn['working'] {
  if (round === null && summary) return {lead: summary, detail: null};
  if (turn.kind === 'judge') {
    return {lead: 'Checking the change against the pass criteria:', detail: passCriteria};
  }
  return {lead: 'Working', detail: null};
}

interface ExecutionRecord {
  prompt: string | null;
  error: string | null;
  /** What an agent that said nothing still reported: its result's reasoning or analysis. */
  note: string | null;
  /** Sequence of its execution start, which orders turns that have not spoken yet. */
  start: number | null;
}

/**
 * Prompts and result notes per execution key, keyed exactly as turns are: execution id, or the
 * legacy occurrence key when the events carry no ids (an execution start opens an occurrence).
 */
function executionRecords(captured: readonly RunEvent[]): Map<string, ExecutionRecord> {
  const keys = new ExecutionKeys(invocationAliases(captured));
  const records = new Map<string, ExecutionRecord>();
  for (const event of captured) {
    const kind = event.data?.kind;
    const opens = kind === 'agent_execution_started';
    const relevant = opens || kind === 'invocation_started' || kind === 'agent_execution_finished';
    if (!relevant || !event.agent_kind) continue;
    const id = event.execution_id ?? event.invocation_id;
    const key = keys.key(id, event.agent_kind, event.round_label ?? null, opens);
    records.set(
      key,
      recordEvent(records.get(key) ?? {prompt: null, error: null, note: null, start: null}, event),
    );
  }
  return records;
}

function recordEvent(record: ExecutionRecord, event: RunEvent): ExecutionRecord {
  const data = event.data;
  if (data?.kind === 'agent_execution_finished') {
    const result = data.result ?? null;
    return {
      ...record,
      error: data.error ?? null,
      note:
        resultText(result, 'reasoning') ??
        resultText(result, 'analysis') ??
        resultText(result, 'summary'),
    };
  }
  if (data?.kind === 'agent_execution_started' || data?.kind === 'invocation_started') {
    return {
      ...record,
      prompt: record.prompt ?? (data.user_prompt?.trim() || null),
      start: record.start ?? event.sequence ?? null,
    };
  }
  return record;
}

function attachVerdicts(captured: readonly RunEvent[], round: number | null, turns: Turn[]): void {
  for (const event of captured) {
    const data = event.data;
    if (data?.kind !== 'judge_result' || roundNumberFromLabel(event.round_label) !== round)
      continue;
    const prefix = event.round_label ?? '';
    const judges = turns.filter(turn => turn.kind === 'judge');
    const judge =
      judges.filter(turn => (turn.label ?? '').startsWith(prefix)).at(-1) ?? judges.at(-1);
    if (judge !== undefined)
      judge.verdict = {accepted: data.verdict === 'pass', feedback: data.feedback};
  }
}

/** A consumed steer goes to the execution its control event names; label and kind only without one. */
function attachSteers(captured: readonly RunEvent[], round: number | null, turns: Turn[]): void {
  const keys = new ExecutionKeys(invocationAliases(captured));
  for (const steer of steers(captured).consumed) {
    if (steer.round !== round) continue;
    const named =
      steer.executionId === null ? undefined : keys.key(steer.executionId, '', null, false);
    // A named execution that matches no turn falls back to the round's last turn, never to
    // another execution that shares its label.
    const byExecution = turns.find(candidate => candidate.id === named);
    const byLabel =
      named === undefined
        ? turns.filter(
            candidate => candidate.kind === steer.agentKind && candidate.label === steer.roundLabel,
          )
        : [];
    const turn = byExecution ?? byLabel.at(-1) ?? turns.at(-1);
    turn?.items.push({kind: 'steer', id: steer.id, text: steer.text});
  }
}

interface JournaledSteer {
  sequence: number;
  text: string;
  consumed: boolean;
}

function journaledSteers(captured: readonly RunEvent[]): JournaledSteer[] {
  const journal: JournaledSteer[] = [];
  for (const event of captured) {
    if (event.type !== 'control' || !event.text?.startsWith('/steer')) continue;
    if (event.status === 'pending' && event.text.startsWith('/steer: ')) {
      journal.push({
        sequence: event.sequence ?? 0,
        text: event.text.slice('/steer: '.length),
        consumed: false,
      });
    } else if (event.status === 'consumed') {
      // One consumed event delivers every steer queued before it.
      for (const steer of journal) steer.consumed = true;
    }
  }
  return journal;
}

/**
 * The steers still waiting for an agent call, with stable identities. Each acknowledged steer
 * claims the first unclaimed journaled steer with its text that arrived after it was sent,
 * whichever of the acknowledgment and the event came first; a claimed steer keeps the sent id.
 * A sent steer nothing journaled yet stays queued until a consumption follows it.
 */
export function queuedSteers(
  captured: readonly RunEvent[],
  sent: readonly SentSteer[],
): QueuedSteer[] {
  const journal = journaledSteers(captured);
  const claims = new Map<JournaledSteer, SentSteer>();
  const local: QueuedSteer[] = [];
  const lastConsumed = captured.reduce(
    (latest, event) =>
      event.type === 'control' && event.status === 'consumed'
        ? Math.max(latest, event.sequence ?? 0)
        : latest,
    -1,
  );
  for (const steer of sent) {
    const match = journal.find(
      entry =>
        !claims.has(entry) && entry.text === steer.text && entry.sequence > steer.afterSequence,
    );
    if (match !== undefined) claims.set(match, steer);
    else if (lastConsumed <= steer.afterSequence) local.push({id: steer.id, text: steer.text});
  }
  const pending = journal
    .filter(entry => !entry.consumed)
    .map(entry => ({id: claims.get(entry)?.id ?? `steer-${entry.sequence}`, text: entry.text}));
  return [...pending, ...local];
}

export type ToolDetail =
  | {kind: 'diff'; lines: DiffLine[]}
  | {
      kind: 'output';
      command: string | null;
      lines: Array<{text: string; failed: boolean}>;
      /** Characters past the cap, grouped; null when nothing was cut. */
      cut: string | null;
      pending: boolean;
    };

/** Past this a tool's output stops being a page; the terminal that produced it serves better. */
const OUTPUT_CAP = 100_000;
const FAILED = /\bFAILED\b|^error(?:\[|:)/;

/** What an expanded tool row shows: an edit as a diff, anything else as its command and output. */
export function toolDetail(core: CoreState, id: string): ToolDetail | null {
  const entry = core.transcript.find(item => item.id === id);
  if (entry === undefined || entry.kind !== 'tool') return null;
  const args = entry.toolArguments ?? {};
  const before = args['old_string'];
  const after = args['new_string'];
  const edit = entry.toolName === 'Edit' || entry.toolName === 'MultiEdit';
  if (edit && typeof before === 'string' && typeof after === 'string') {
    return {kind: 'diff', lines: lineDiff(before, after)};
  }
  const payload = entry.toolResult?.payload;
  const whole =
    payload?.kind === 'command'
      ? [payload.stdout, payload.stderr]
          .map(stream => stream.replace(/\n$/, ''))
          .filter(stream => stream !== '')
          .join('\n\n')
      : (entry.toolResult?.content ?? entry.toolResponse ?? '');
  const command = args['command'];
  return {
    kind: 'output',
    command: typeof command === 'string' ? command : null,
    lines: whole
      .slice(0, OUTPUT_CAP)
      .split('\n')
      .map(text => ({text, failed: FAILED.test(text)})),
    cut: whole.length > OUTPUT_CAP ? formatValue(whole.length - OUTPUT_CAP) : null,
    pending: entry.toolResult === undefined && entry.toolResponse === undefined,
  };
}

/** A one-hunk diff: the shared prefix and suffix as context, the middle as removed then added. */
export function lineDiff(before: string, after: string): DiffLine[] {
  const old = before.replace(/\n$/, '').split('\n');
  const next = after.replace(/\n$/, '').split('\n');
  let head = 0;
  while (head < old.length && head < next.length && old[head] === next[head]) head += 1;
  let tail = 0;
  while (
    tail < old.length - head &&
    tail < next.length - head &&
    old[old.length - 1 - tail] === next[next.length - 1 - tail]
  ) {
    tail += 1;
  }
  const as =
    (tone: DiffLine['tone']) =>
    (text: string): DiffLine => ({tone, text, line: null});
  return [
    ...old.slice(0, head).map(as('ctx')),
    ...old.slice(head, old.length - tail).map(as('del')),
    ...next.slice(head, next.length - tail).map(as('add')),
    ...old.slice(old.length - tail).map(as('ctx')),
  ];
}
