/** The unfinished New run of a window, kept in the window's own (session) storage. */
import {DRAFT_KEY, parseDraft, storeDraft} from '../../state/index.js';
import type {DraftMemory} from './store.js';

/**
 * The memory over browser storage. Storage can be missing, blocked or full, so every access is
 * guarded: `storage` may throw or return null, and then nothing is kept.
 */
export function browserDraftMemory(storage: () => Storage | null): DraftMemory {
  return {
    read() {
      try {
        return parseDraft(storage()?.getItem(DRAFT_KEY) ?? null);
      } catch {
        return null;
      }
    },
    write(draft) {
      try {
        if (draft === null) storage()?.removeItem(DRAFT_KEY);
        else storage()?.setItem(DRAFT_KEY, storeDraft(draft));
      } catch {
        // The draft lasts this run only.
      }
    },
  };
}
