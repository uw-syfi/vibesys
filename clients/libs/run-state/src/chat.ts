/**
 * Experiment-chat threads in the run fold: registration, titles, and each thread's transcript.
 */
import type {RunEvent, RunSnapshot} from '@vibesys/contracts';
import {acceptsRunMetadata, retainedReplayEvents} from './replay.js';
import {
  type ChatProposal,
  type ChatThread,
  type CoreState,
  cloneCoreStateWith,
  DEFAULT_CHAT_THREAD_ID,
} from './state.js';
import {
  eventToTranscriptEntry,
  foldTranscriptEntry,
  type TranscriptEntry,
} from './transcript-fold.js';
import type {TranscriptFolder} from './transcript-folder.js';

/** The snapshot's thread registry and last thread, applied to `state`. */
export function registerSnapshotChat(state: CoreState, snapshot: RunSnapshot): CoreState {
  const registered = (snapshot.chat_threads ?? []).reduce(
    (current, thread) =>
      upsertChatThread(current, {
        id: thread.thread_id,
        title: thread.title ?? '',
        provider: thread.provider,
        model: thread.model,
      }),
    state,
  );
  const lastChatThreadId = snapshot.last_chat_thread_id ?? registered.lastChatThreadId;
  return lastChatThreadId === registered.lastChatThreadId
    ? registered
    : cloneCoreStateWith(registered, {lastChatThreadId});
}

/** Adds snapshot-only thread registry facts without merging an event projection. */
export function retainRegisteredChatThreads(
  state: CoreState,
  registered: readonly ChatThread[],
): CoreState {
  return registered.reduce(retainRegisteredChatThread, state);
}

function retainRegisteredChatThread(state: CoreState, thread: ChatThread): CoreState {
  const existing = state.chatThreads.find(candidate => candidate.id === thread.id);
  if (existing === undefined) return upsertChatThread(state, thread);
  const retained = {
    ...thread,
    title: thread.title || existing.title,
    provider: thread.provider ?? existing.provider,
    model: thread.model ?? existing.model,
  };
  return retained.title === existing.title &&
    retained.provider === existing.provider &&
    retained.model === existing.model
    ? state
    : cloneCoreStateWith(state, {
        chatThreads: state.chatThreads.map(candidate =>
          candidate.id === thread.id ? retained : candidate,
        ),
      });
}

export function applyChatEvent(
  state: CoreState,
  event: RunEvent,
  folder: TranscriptFolder | null,
): CoreState {
  const data = event.data;
  const threadId = event.chat_thread_id ?? DEFAULT_CHAT_THREAD_ID;
  if (data?.kind === 'chat_thread_created') {
    return upsertChatThread(
      state,
      {
        id: data.thread_id,
        title: data.title ?? '',
        provider: data.provider,
        model: data.model,
      },
      event.sequence,
    );
  }
  let next = state;
  const typed = data?.kind === 'tool_call' || data?.kind === 'tool_result';
  if (typed && next.chatTypedToolEvents[threadId] !== true) {
    next = cloneCoreStateWith(next, {
      chatTypedToolEvents: {...next.chatTypedToolEvents, [threadId]: true},
    });
  }
  const legacyToolChunk =
    data?.kind === 'agent_output_chunk' &&
    data.channel === 'tool' &&
    next.chatTypedToolEvents[threadId] === true;
  if (legacyToolChunk) return next;
  if (data?.kind === 'chat' && data.thread_title) {
    next = setChatThreadTitle(next, threadId, data.thread_title);
  }
  if (data?.kind === 'chat') next = recordChatQuestion(next, threadId, event);
  const entry = eventToTranscriptEntry(event);
  if (entry === null || (entry.kind !== 'assistant' && entry.kind !== 'result')) return next;
  return appendChatTranscript(next, threadId, entry, folder, data?.kind === 'chat');
}

/** Keeps the question the terminal `chat` event carries, once per event. */
function recordChatQuestion(state: CoreState, threadId: string, event: RunEvent): CoreState {
  const text = event.text ?? '';
  const sequence = event.sequence ?? 0;
  if (text === '' || sequence <= 0) return state;
  const id = String(sequence);
  const known = state.chatQuestions[threadId] ?? [];
  if (known.some(question => question.id === id)) return state;
  return cloneCoreStateWith(state, {
    chatQuestions: {...state.chatQuestions, [threadId]: [...known, {id, text}]},
  });
}

/**
 * Keeps a steer the thread's agent proposed, once per `proposal_id`. A proposal records nothing
 * about delivery: the operator's `command.control` is the only thing that reaches the run.
 */
export function applyChatProposal(state: CoreState, event: RunEvent, sequence: number): CoreState {
  const data = event.data;
  if (data?.kind !== 'steer_proposed') return state;
  const threadId = event.chat_thread_id ?? DEFAULT_CHAT_THREAD_ID;
  const known = state.chatProposals[threadId] ?? [];
  if (known.some(proposal => proposal.id === data.proposal_id)) return state;
  const proposal: ChatProposal = {
    id: data.proposal_id,
    sequence,
    text: data.proposal.text,
    target: {
      kind: data.proposal.target?.kind ?? 'run',
      id: data.proposal.target?.id ?? null,
    },
    mode: data.proposal.mode ?? 'next_turn',
  };
  return cloneCoreStateWith(state, {
    chatProposals: {...state.chatProposals, [threadId]: [...known, proposal]},
  });
}

/** Registers a replayed thread, or refreshes the record it already has. */
function upsertChatThread(
  state: CoreState,
  thread: ChatThread,
  creationSequence?: number,
): CoreState {
  const existing = state.chatThreads.find(candidate => candidate.id === thread.id);
  let chatThreads =
    existing === undefined
      ? [...state.chatThreads, thread]
      : state.chatThreads.map(candidate =>
          candidate.id === thread.id
            ? {...thread, title: thread.title || candidate.title}
            : candidate,
        );
  if (creationSequence !== undefined && creationSequence > 0) {
    chatThreads = orderChatThreadsByCreation(state, chatThreads, thread.id, creationSequence);
  }
  const chatTranscripts =
    state.chatTranscripts[thread.id] === undefined
      ? {...state.chatTranscripts, [thread.id]: []}
      : state.chatTranscripts;
  return cloneCoreStateWith(state, {chatThreads, chatTranscripts});
}

/**
 * Restores the server's creation order when independent RPC replies arrive in
 * reverse order. Response-route deliveries are retained until their stream
 * copies arrive, so reducer provenance, rather than a caller-local sort, owns
 * the authoritative journal sequence for every observed creation event.
 */
function orderChatThreadsByCreation(
  state: CoreState,
  threads: readonly ChatThread[],
  currentThreadId: string,
  currentSequence: number,
): ChatThread[] {
  const creationSequences = new Map<string, number>();
  for (const {event} of retainedReplayEvents(state)) {
    const data = event.data;
    const sequence = event.sequence ?? 0;
    if (
      data?.kind !== 'chat_thread_created' ||
      sequence <= 0 ||
      !acceptsRunMetadata(state.runId, [event])
    ) {
      continue;
    }
    const previous = creationSequences.get(data.thread_id);
    creationSequences.set(data.thread_id, Math.min(previous ?? sequence, sequence));
  }
  const previous = creationSequences.get(currentThreadId);
  creationSequences.set(currentThreadId, Math.min(previous ?? currentSequence, currentSequence));

  const defaultThread = threads.find(thread => thread.id === DEFAULT_CHAT_THREAD_ID);
  const registered: Array<{thread: ChatThread; index: number}> = [];
  const replayed: Array<{thread: ChatThread; index: number; sequence: number}> = [];
  for (const [index, candidate] of threads.entries()) {
    if (candidate.id === DEFAULT_CHAT_THREAD_ID) continue;
    const sequence = creationSequences.get(candidate.id);
    if (sequence === undefined) registered.push({thread: candidate, index});
    else replayed.push({thread: candidate, index, sequence});
  }
  replayed.sort((left, right) => left.sequence - right.sequence || left.index - right.index);
  return [
    ...(defaultThread === undefined ? [] : [defaultThread]),
    ...registered.map(item => item.thread),
    ...replayed.map(item => item.thread),
  ];
}

function setChatThreadTitle(state: CoreState, threadId: string, title: string): CoreState {
  if (!state.chatThreads.some(thread => thread.id === threadId)) {
    // A titled turn for a thread whose creation event is missing from the
    // replay window still names a thread the operator can select.
    return setChatThreadTitle(
      upsertChatThread(state, {id: threadId, title: '', provider: null, model: null}),
      threadId,
      title,
    );
  }
  return cloneCoreStateWith(state, {
    chatThreads: state.chatThreads.map(thread =>
      thread.id === threadId ? {...thread, title} : thread,
    ),
  });
}

function appendChatTranscript(
  state: CoreState,
  threadId: string,
  entry: TranscriptEntry,
  folder: TranscriptFolder | null,
  finalAnswer = false,
): CoreState {
  if (folder !== null) {
    const buffer = folder.buffer(threadId, state.chatTranscripts[threadId] ?? []);
    if (!(finalAnswer && foldChatAnswer(buffer.entries, entry))) buffer.append(entry);
    return state;
  }
  const transcript = [...(state.chatTranscripts[threadId] ?? [])];
  if (!(finalAnswer && foldChatAnswer(transcript, entry))) {
    foldTranscriptEntry(transcript, entry, null);
  }
  const chatTranscripts = {...state.chatTranscripts, [threadId]: transcript};
  return cloneCoreStateWith(state, {
    chatTranscripts,
    chatTranscript: threadId === DEFAULT_CHAT_THREAD_ID ? transcript : state.chatTranscript,
  });
}

/**
 * Folds a turn's terminal answer over its own streamed chunks.
 *
 * The assistant chunks of one chat turn have already merged into a single
 * entry keyed by the turn's invocation id, and the terminal `chat` event
 * carries the same text once more. When such a turn is still open at the end
 * of the transcript, the final answer replaces it in place rather than
 * appearing as a second copy. The streamed entry's id is kept so consumers
 * tracking entries by id update instead of duplicating, and the turn id is
 * dropped because the turn is over: neither a later chunk nor a later answer
 * may fold into it. Returns false when there is no open streamed turn, in
 * which case the answer appends as its own entry.
 *
 * An answer stamped with an invocation id owns exactly the turn that streamed
 * under that id: a mismatch means the open turn was abandoned (its invocation
 * failed before a terminal answer was recorded), so the answer appends and
 * the abandoned turn stays as it streamed. Answers from journals written
 * before the id existed carry none and keep the last-open-turn fold.
 */
function foldChatAnswer(entries: TranscriptEntry[], incoming: TranscriptEntry): boolean {
  const last = entries.at(-1);
  if (last === undefined || last.kind !== 'assistant' || last.turnId === undefined) return false;
  if (incoming.invocationId !== undefined && incoming.invocationId !== last.invocationId) {
    return false;
  }
  const {turnId: _closed, ...merged} = {...last, ...incoming, id: last.id};
  entries[entries.length - 1] = merged;
  return true;
}
