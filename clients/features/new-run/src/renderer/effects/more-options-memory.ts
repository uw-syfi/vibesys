/** What the user chose for More options on each machine, kept in the viewer's browser storage. */
import type {MoreOptionsMemory} from './store.js';

export const MORE_OPTIONS_KEY = 'vibesys.new-run.more-options';

/** The choices in `text`: an object of booleans by machine; anything else is no choices. */
function parseChoices(text: string | null): Record<string, boolean> {
  if (text === null) return {};
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
    return Object.fromEntries(
      Object.entries(value).filter(
        (entry): entry is [string, boolean] => typeof entry[1] === 'boolean',
      ),
    );
  } catch {
    return {};
  }
}

/**
 * The memory over browser storage. Storage can be missing, blocked or full, so every access is
 * guarded: `storage` may throw or return null, and then nothing is kept.
 */
export function browserMoreOptionsMemory(storage: () => Storage | null): MoreOptionsMemory {
  return {
    read() {
      try {
        return parseChoices(storage()?.getItem(MORE_OPTIONS_KEY) ?? null);
      } catch {
        return {};
      }
    },
    write(choices) {
      try {
        storage()?.setItem(MORE_OPTIONS_KEY, JSON.stringify(choices));
      } catch {
        // The choices last this run only.
      }
    },
  };
}
