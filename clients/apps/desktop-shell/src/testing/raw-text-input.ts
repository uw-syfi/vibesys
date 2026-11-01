/**
 * The words the raw-text check reads against: the closed sets of the run (failure reasons, the
 * registries' ids and names) and the names a fixture world's user typed. One definition for the
 * browser journeys' check and the DOM tests that hold the same wording (`raw-text.ts`).
 */
import {CODE_IDS, roleName} from '@vibesys/contracts';
import {FAILURE_REASONS} from '@vibesys/run-fixtures';
import type {RawTextInput} from './raw-text.js';

/** Enum values that never read as words: the failure reasons spelled with a hyphen or underscore. */
const RAW_WORDS: readonly string[] = FAILURE_REASONS.filter(reason => /[-_]/.test(reason));

/** The ids the backend registries name, so the list of what must not show raw cannot drift. */
const REGISTRY_IDS: readonly string[] = [
  ...CODE_IDS.harnesses,
  ...CODE_IDS.models,
  ...CODE_IDS.roles,
];

/** The role names the catalog gives: none may be drawn in lowercase. */
const REGISTRY_NAMES: readonly string[] = CODE_IDS.roles.map(roleName);

/**
 * The names the fixture worlds' user typed: a project folder, an SSH alias, a task. They are the
 * user's own identifiers, shown as they were typed, not code names the app made up.
 */
const USER_NAMES: readonly string[] = [
  ...Array.from({length: 9}, (_, index) => `queue-rs-run${index + 1}`),
  'queue-rs',
  'queue-rs-2',
  'remote-host',
  'host-a',
  'host-b',
  'host-c',
  // The clusters and SSH aliases the machine Settings journeys type or list.
  'lab-h100',
  'lab-login',
  'amd-login',
  'cluster-m',
  'no-workspace',
  'no-scheduler',
  // An example's own name, which New run shows in the code face as the catalogue names it.
  'kv-store',
];

export const RAW_TEXT_INPUT: RawTextInput = {
  rawWords: RAW_WORDS,
  ids: REGISTRY_IDS,
  names: REGISTRY_NAMES,
  userNames: USER_NAMES,
};
