/**
 * The Ask tab: experiment-chat threads, their questions and answers, and the models the run
 * offers. Questions come from recorded `chat` events the session captured, plus the session's own
 * asks not yet seen recorded.
 */
import type {ChatOptions, RunEvent} from '@vibesys/backend-client';
import {type ChatThread, DEFAULT_CHAT_THREAD_ID, type TranscriptEntry} from '@vibesys/core-state';
import {prose} from './derive.js';
import type {ProsePart} from './model.js';
import type {QueryState, SentAsk} from './session.js';

export interface AskMessage {
  id: string;
  question: string;
  answer: ProsePart[][] | null;
  error: string | null;
}

export interface ThreadRow {
  id: string;
  title: string;
  /** Who answers: the thread's own selection, or the run's default for the implicit thread. */
  provider: string | null;
  model: string | null;
  count: number;
}

export interface ModelGroup {
  provider: string;
  label: string;
  models: string[];
}

export interface AskView {
  harness: 'available' | 'checking' | 'failed' | 'none';
  threads: ThreadRow[];
  current: ThreadRow;
  messages: AskMessage[];
  /** A question on this thread waits for its answer: Send holds until it arrives. */
  pending: boolean;
  /** The answer streamed so far while the question waits. */
  streaming: ProsePart[][] | null;
  groups: ModelGroup[];
}

export interface AskInput {
  threads: readonly ChatThread[];
  transcripts: Readonly<Record<string, readonly TranscriptEntry[]>>;
  captured: readonly RunEvent[];
  asks: readonly SentAsk[];
  options: ChatOptions | null;
  /** The options query has not answered yet. */
  checking: boolean;
  /** The options query's last attempt failed. */
  failed: boolean;
  selected: string;
  /** The model the selected thread was created on here, until its record arrives. */
  picked: {provider: string; model: string} | null;
}

const HARNESS_NAMES: Readonly<Record<string, string>> = {
  claude: 'Claude Code',
  codex: 'Codex',
  gemini: 'Gemini',
  opencode: 'Opencode',
};

const waiting = (ask: SentAsk): boolean => ask.answer === null && ask.error === null;

function recordedIn(captured: readonly RunEvent[], thread: string): RunEvent[] {
  return captured.filter(
    event => event.type === 'chat' && (event.chat_thread_id ?? DEFAULT_CHAT_THREAD_ID) === thread,
  );
}

/** Asks not yet seen as a question recorded after they were sent (the stream can beat the response). */
function unrecorded(asks: readonly SentAsk[], recorded: readonly RunEvent[]): SentAsk[] {
  const claimed = new Set<number>();
  return asks.filter(ask => {
    const match = recorded.find(event => {
      const sequence = event.sequence ?? 0;
      return event.text === ask.text && sequence > ask.afterSequence && !claimed.has(sequence);
    });
    if (match === undefined) return true;
    claimed.add(match.sequence ?? 0);
    return false;
  });
}

function answerOf(event: RunEvent): string {
  const data = event.data;
  return data?.kind === 'chat' ? data.answer : '';
}

function messagesOf(input: AskInput, thread: string): AskMessage[] {
  const recorded = recordedIn(input.captured, thread);
  const answered = recorded.map(event => ({
    id: `chat-${event.sequence ?? 0}`,
    question: event.text ?? '',
    answer: prose(answerOf(event)),
    error: null,
  }));
  const mine = input.asks.filter(ask => ask.threadId === thread);
  const local = unrecorded(mine, recorded).map(ask => ({
    id: ask.id,
    question: ask.text,
    answer: ask.answer === null ? null : prose(ask.answer),
    error: ask.error,
  }));
  return [...answered, ...local];
}

function runDefault(options: ChatOptions | null): Pick<ThreadRow, 'provider' | 'model'> {
  for (const group of options?.providers ?? []) {
    const option = group.models?.find(candidate => candidate.default === true);
    if (option !== undefined) return {provider: group.provider, model: option.model};
  }
  return {provider: null, model: null};
}

function rowOf(input: AskInput, thread: ChatThread): ThreadRow {
  const messages = messagesOf(input, thread.id);
  const runtime =
    thread.model === null
      ? runDefault(input.options)
      : {provider: thread.provider, model: thread.model};
  const first = messages[0]?.question.split('\n')[0];
  return {
    id: thread.id,
    title: thread.title || first || 'New thread',
    ...runtime,
    count: messages.length,
  };
}

function modelGroups(options: ChatOptions | null): ModelGroup[] {
  return (options?.providers ?? [])
    .map(group => ({
      provider: group.provider,
      label: `${HARNESS_NAMES[group.provider] ?? group.provider} harness`,
      models: (group.models ?? []).map(option => option.model),
    }))
    .filter(group => group.models.length > 0);
}

/**
 * The options query as Ask reads it. A failure is never "none offered"; a re-check keeps the last
 * answer on screen until the new one lands.
 */
export function chatOffer(query: QueryState): Pick<AskInput, 'options' | 'checking' | 'failed'> {
  return {
    options: query.response?.chat_options ?? null,
    checking: query.response === null,
    failed: query.error !== null && !query.loading,
  };
}

export function askView(input: AskInput): AskView {
  const selected: ChatThread = input.threads.find(thread => thread.id === input.selected) ?? {
    id: input.selected,
    title: '',
    driver: null,
    provider: input.picked?.provider ?? null,
    model: input.picked?.model ?? null,
  };
  // A thread created a moment ago can be selected before its record reaches the page.
  const threads = input.threads.includes(selected) ? input.threads : [...input.threads, selected];
  const pending = input.asks.some(ask => ask.threadId === selected.id && waiting(ask));
  const open = input.transcripts[selected.id]?.at(-1);
  const streamed = pending && open?.kind === 'assistant' && open.turnId !== undefined;
  const offered = (input.options?.providers ?? []).length > 0;
  return {
    harness: offered ? 'available' : input.failed ? 'failed' : input.checking ? 'checking' : 'none',
    threads: threads.map(thread => rowOf(input, thread)),
    current: rowOf(input, selected),
    messages: messagesOf(input, selected.id),
    pending,
    streaming: streamed && open !== undefined ? prose(open.content) : null,
    groups: modelGroups(input.options),
  };
}
