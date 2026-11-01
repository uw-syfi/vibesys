/**
 * The settings feature's pure state: the Settings screen's store and the theme it resolves.
 */

export {
  type AcceleratorChoice,
  buildCluster,
  type ClusterFields,
  type ClusterForm,
  canSave,
  EMPTY_FIELDS,
  type FormField,
  type FormProblem,
  type FormTest,
  NEW_FORM,
  type ReachKind,
  stale,
  statedHardware,
} from './cluster-form.js';
export {
  INITIAL_MACHINE_SETTINGS,
  type LoginProgress,
  type MachineSettingsAction,
  type MachineSettingsCall,
  type MachineSettingsState,
  type MachineSettingsStep,
  type MachineView,
  machineSettingsAwaiting,
  machineSettingsInvariants,
  machineSettingsStep,
  type Reading,
  type SettingsSection,
  shownMachine,
} from './machine-settings.js';
export {
  failureWords,
  formFailureWord,
  hardwareModels,
  hardwareWords,
  type LoginSteps,
  loginSteps,
  loginWords,
  machinesDetail,
  modalWords,
  reachWords,
  sentenceCase,
  signInBlocked,
  testFailureWords,
} from './machine-words.js';
export {machineSettingsProblems, settingsProblems} from './problems.js';
export {
  initialSettings,
  type SettingsAction,
  type SettingsChange,
  type SettingsRequest,
  type SettingsState,
  type SettingsStep,
  settingsAwaiting,
  settingsInvariants,
  settingsStep,
  shownTheme,
} from './settings.js';
