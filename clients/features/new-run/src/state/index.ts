/** The New run feature's pure state: the form and its rules, and the screen's machine. */

export type {Check, MachineDescription} from '@vibesys/contracts/host-helper';
export {
  type AgentChoice,
  advancedProblem,
  type Budget,
  type ChecksFor,
  checksPass,
  DEFAULT_BUDGET,
  defaultAgent,
  type Effort,
  type FactsFor,
  type Form,
  inputsKey,
  type ProjectChoice,
  prefill,
  projectPath,
  ROLES,
  type RoleChoice,
  runInputs,
  splitWords,
  vettedModels,
} from './form.js';
export {
  CHECK_DEBOUNCE_MS,
  type NewRunContext,
  type NewRunEvent,
  type NewRunInput,
  type NewRunSnapshot,
  newRunMachine,
  type ProjectFix,
} from './machine.js';
