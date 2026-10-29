/**
 * `@vibesys/run-state/transcript`: the transcript model the run fold produces (the run's
 * transcript and each experiment-chat thread's), as read-only data.
 */
export {type ChatThread, DEFAULT_CHAT_THREAD_ID} from '../state.js';
export type {ToolResultPayload, TranscriptEntry} from '../transcript-fold.js';
export {transcriptBySpan} from '../transcript-spans.js';
export {
  appendNativeTranscript,
  emptyNativeTranscript,
  type NativeTranscript,
  nativeTranscriptEntries,
  nativeTranscriptHashes,
} from './native.js';
