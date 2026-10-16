/** The New run feature's pure state: the form and its rules, and the screen's machine. */

export type {Backend, Check, MachineDescription} from '@vibesys/contracts/host-helper';
export {
  type AgentChoice,
  advancedProblem,
  againForm,
  type Budget,
  type ChecksFor,
  checksPass,
  DEFAULT_BUDGET,
  defaultAgent,
  defaultRole,
  type Effort,
  type FactsFor,
  type Form,
  type HelperFor,
  inputsKey,
  joinWords,
  lastRun,
  missingFeatures,
  NEW_RUN_FEATURES,
  offers,
  type ProjectChoice,
  prefill,
  projectPath,
  type RoleChoice,
  recordedAgent,
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
export {
  agentWithRoles,
  formWithRoles,
  roleLabel,
  roleSummary,
  rolesAfter,
} from './roles.js';
