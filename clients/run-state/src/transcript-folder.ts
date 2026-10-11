/**
 * The transcripts one batch touched, each folded once and published on the batch's final state.
 */
import {type CoreState, cloneCoreStateWith, DEFAULT_CHAT_THREAD_ID} from './state.js';
import {RUN_TRANSCRIPT, TranscriptBuffer, type TranscriptEntry} from './transcript-fold.js';

/** The transcripts one `reduceEventBatch` call touched, folded once each. */
export class TranscriptFolder {
  readonly #buffers = new Map<string, TranscriptBuffer>();

  buffer(key: string, initial: readonly TranscriptEntry[]): TranscriptBuffer {
    const existing = this.#buffers.get(key);
    if (existing !== undefined) return existing;
    const created = new TranscriptBuffer(initial);
    this.#buffers.set(key, created);
    return created;
  }

  /** Publishes every folded transcript onto the batch's final state. */
  commit(state: CoreState): CoreState {
    if (this.#buffers.size === 0) return state;
    const run = this.#buffers.get(RUN_TRANSCRIPT);
    let next = run === undefined ? state : cloneCoreStateWith(state, {transcript: run.entries});
    const threads = [...this.#buffers].filter(([key]) => key !== RUN_TRANSCRIPT);
    if (threads.length === 0) return next;
    const chatTranscripts = {...next.chatTranscripts};
    for (const [threadId, buffer] of threads) chatTranscripts[threadId] = buffer.entries;
    next = cloneCoreStateWith(next, {
      chatTranscripts,
      chatTranscript: chatTranscripts[DEFAULT_CHAT_THREAD_ID] ?? next.chatTranscript,
    });
    return next;
  }
}
