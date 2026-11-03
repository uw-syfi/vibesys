/**
 * The names a person reads for VibeSys's own vocabularies. An agent role (`perf-evaluator`) reads
 * as its title ("Perf evaluator"). The catalog is generated from the Python declarations that own
 * it (`src/generated/display-names.generated.ts`), so no screen maps an id itself; `roleName` only
 * looks an id up, and reads a role a plugin declares as the words of its id.
 *
 * Harness and model names are agentshim's, served by the backend; nothing here names them.
 * `CODE_IDS` lists their ids (and the roles') for the screen checks, which fail on a screen that
 * draws one as text.
 */
import {CODE_IDS, DISPLAY_NAMES} from './generated/display-names.generated.js';

export {CODE_IDS};

const ROLES: Readonly<Record<string, string>> = DISPLAY_NAMES.roles;

/**
 * An id as words in sentence case (`multi-implementer`: "Multi implementer"). Text that already
 * has a space or a capital is words, not an id, and stays as it is ("Lock-free ring").
 */
export function wordsOfId(id: string): string {
  if (/[\sA-Z]/.test(id)) return id;
  const text = id
    .split(/[-_.\s]+/)
    .filter(word => word !== '')
    .join(' ');
  return text === '' ? id : text.charAt(0).toUpperCase() + text.slice(1);
}

/** The title of a role the catalog declares, or null for any other text (an idea's headline). */
export function declaredRoleName(role: string): string | null {
  return ROLES[role] ?? null;
}

/** An agent role a person reads: "Reviewer", "Perf evaluator". */
export function roleName(role: string): string {
  return ROLES[role] ?? wordsOfId(role);
}
