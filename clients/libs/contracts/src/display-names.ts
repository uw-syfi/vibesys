/**
 * The names a person reads for the closed vocabularies a screen shows: a harness (`claude` is
 * "Claude Code"), a model (its vendor id reads "Opus 5.5") and an agent role (`perf-evaluator` is
 * "Perf evaluator"). The catalog is generated from the Python registries that own each fact
 * (`src/generated/display-names.generated.ts`), so no screen maps an id itself; these functions
 * only look an id up and say what a screen shows for one no registry names:
 *
 * - a role a plugin declares reads as the words of its id, in sentence case;
 * - a harness reads the same way, with its first letter capitalised;
 * - a model no registry names stays its identifier, which is what it is: `modelName` returns it
 *   unchanged, and `isNamedModel` says whether a registry named it.
 */
import {DISPLAY_NAMES} from './generated/display-names.generated.js';

/** The ids the registries name, for a check that the screens show none of them raw. */
export const DISPLAY_IDS = {
  harnesses: Object.keys(DISPLAY_NAMES.harnesses),
  models: Object.keys(DISPLAY_NAMES.models),
  roles: Object.keys(DISPLAY_NAMES.roles),
} as const;

const NAMES: {
  readonly harnesses: Readonly<Record<string, string>>;
  readonly models: Readonly<Record<string, string>>;
  readonly roles: Readonly<Record<string, string>>;
} = DISPLAY_NAMES;

/** An id as words in sentence case (`multi-implementer`: "Multi implementer"). */
export function wordsOfId(id: string): string {
  const text = id
    .split(/[-_.\s]+/)
    .filter(word => word !== '')
    .join(' ');
  return text === '' ? id : text.charAt(0).toUpperCase() + text.slice(1);
}

/** The harness (agent CLI provider) a person reads: "Claude Code", "Codex". */
export function harnessName(provider: string): string {
  return NAMES.harnesses[provider] ?? wordsOfId(provider);
}

/** The model a person reads ("Opus 5.5"); a model no registry names stays its identifier. */
export function modelName(model: string): string {
  return NAMES.models[model] ?? model;
}

/** Whether a registry names `model`; when not, `modelName` returns the identifier itself. */
export function isNamedModel(model: string): boolean {
  return model in NAMES.models;
}

/** An agent role a person reads: "Judge", "Perf evaluator". */
export function roleName(role: string): string {
  return NAMES.roles[role] ?? wordsOfId(role);
}
