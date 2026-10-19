/**
 * The chat as the panel reads it: the threads of the run fold, and one thread's items in order
 * (the user's questions, the agent's answers as they stream, and the steers it proposed). Pure
 * selectors over `CoreState`; the fold itself is `@vibesys/run-state`'s.
 *
 * The fold keeps a question only when its answer is complete (the terminal `chat` event carries
 * both), so the question being answered now is the caller's `pending` text, shown first and
 * superseded once the fold has it. Questions and answers pair by order: the k-th question goes
 * before the k-th answer.
 *
 * Every item of a thread has an id no other item has: the thread draws rows keyed by it and keeps
 * each row's measured height under it. The fold names a question and the answer that ends its turn
 * by the one event that carried both (the answer keeps the question's sequence when nothing
 * streamed before it), so a question's id carries a prefix of its own.
 */
import type {ChatProposal, CoreState} from '@vibesys/run-state';

const DEFAULT_THREAD = 'default';

export interface ThreadEntry {
  readonly id: string;
  /** The backend's title, or the placeholder the panel shows for a thread with none. */
  readonly title: string;
  /** The provider and model the thread runs, as ids; null for the run's own. */
  readonly provider: string | null;
  readonly model: string | null;
  readonly answers: number;
}

export type ThreadItem =
  | {readonly kind: 'question'; readonly id: string; readonly text: string}
  | {
      readonly kind: 'answer';
      readonly id: string;
      readonly text: string;
      /** The agent is still writing this answer. */
      readonly streaming: boolean;
    }
  | {readonly kind: 'proposal'; readonly proposal: ChatProposal}
  /** The question went out and nothing has come back yet. */
  | {readonly kind: 'waiting'; readonly id: string};

/**
 * What the Threads menu calls a thread the backend has not titled. It is never "New chat": that is
 * the action that opens an empty thread, and an existing thread listed under its name was opened
 * by people who meant the action, showing them another thread's transcript.
 */
const UNTITLED_THREAD = 'Untitled';

/** The run's threads, newest first. The implicit default thread shows only once it has answers. */
export function threadEntries(core: CoreState): readonly ThreadEntry[] {
  return core.chatThreads
    .filter(
      thread =>
        thread.id !== DEFAULT_THREAD || (core.chatTranscripts[DEFAULT_THREAD]?.length ?? 0) > 0,
    )
    .map(thread => ({
      id: thread.id,
      title: thread.title === '' ? UNTITLED_THREAD : thread.title,
      provider: thread.provider,
      model: thread.model,
      answers: core.chatTranscripts[thread.id]?.length ?? 0,
    }))
    .reverse();
}

/** What the user chose to look at: the run's last thread until they pick another or a new chat. */
export type ThreadChoice =
  | {readonly kind: 'last'}
  | {readonly kind: 'thread'; readonly id: string}
  | {readonly kind: 'new'};

/** The thread shown: the chosen one, or the last the server named, else the newest; null for a new chat. */
export function shownThread(core: CoreState, choice: ThreadChoice): string | null {
  if (choice.kind === 'new') return null;
  const known = new Set(core.chatThreads.map(thread => thread.id));
  if (choice.kind === 'thread') return known.has(choice.id) ? choice.id : null;
  const entries = threadEntries(core);
  if (core.lastChatThreadId !== null && entries.some(entry => entry.id === core.lastChatThreadId)) {
    return core.lastChatThreadId;
  }
  return entries[0]?.id ?? null;
}

function sequenceOf(id: string): number {
  const sequence = Number(id);
  return Number.isFinite(sequence) ? sequence : Number.POSITIVE_INFINITY;
}

/**
 * One thread's items, in order. `pending` is the question sent and not yet in the fold, if any.
 * A proposal sits after the last answer that began before it.
 */
export function threadItems(
  core: CoreState,
  threadId: string,
  pending: string | null,
): readonly ThreadItem[] {
  const answers = core.chatTranscripts[threadId] ?? [];
  const questions = core.chatQuestions[threadId] ?? [];
  const proposals = [...(core.chatProposals[threadId] ?? [])].sort(
    (left, right) => left.sequence - right.sequence,
  );
  const items: ThreadItem[] = [];
  let next = 0;
  const flushProposals = (before: number): void => {
    while (next < proposals.length && (proposals[next]?.sequence ?? 0) < before) {
      const proposal = proposals[next];
      if (proposal !== undefined) items.push({kind: 'proposal', proposal});
      next += 1;
    }
  };
  answers.forEach((answer, index) => {
    flushProposals(sequenceOf(answer.id));
    const question = questions[index];
    if (question !== undefined) {
      items.push({kind: 'question', id: `question ${question.id}`, text: question.text});
    } else if (index === questions.length && pending !== null) {
      items.push({kind: 'question', id: `pending ${index}`, text: pending});
    }
    items.push({
      kind: 'answer',
      id: answer.id,
      text: answer.content,
      streaming: answer.turnId !== undefined,
    });
  });
  if (pending !== null && answers.length <= questions.length) {
    items.push({kind: 'question', id: `pending ${answers.length}`, text: pending});
    items.push({kind: 'waiting', id: `waiting ${answers.length}`});
  }
  flushProposals(Number.POSITIVE_INFINITY);
  return items;
}
