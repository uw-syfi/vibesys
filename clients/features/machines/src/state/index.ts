/**
 * The machines feature's pure state: the Machines screen's store (the saved machines, each one's
 * status from its facts, the Add form and the checkout form with path suggestions) and the words
 * and facts the screen shows.
 */

export {AGE_TICK_MS, clockMachine} from './clock.js';
export {gpuLine, gpuName} from './hardware.js';
export {
  type FormName,
  initialMachines,
  type Machine,
  type MachineStatus,
  type MachinesAction,
  type MachinesRequest,
  type MachinesState,
  type MachinesStep,
  machineCounts,
  machineStatus,
  machinesAwaited,
  machinesInvariants,
  machinesStep,
  selectedMachine,
} from './machines.js';
export {
  cardAction,
  cardStatus,
  clockWord,
  hardwareLine,
  type Load,
  machineHardware,
  machineLoad,
  machinesAttention,
  type PlaceRow,
  placeRows,
  placesReading,
} from './overview.js';
export {formSuggestions} from './path-form.js';
export type {ClusterCheck, LastCheck, Places, PlacesReading} from './places.js';
export {machinesProblems} from './problems.js';
export {
  ageWord,
  FAILURE_WORDS,
  type GaugeView,
  type StatsView,
  statsGauges,
  statsView,
} from './stats.js';
export {
  type MachineFact,
  machineFacts,
  machineProblem,
  machineSummary,
  type StatusTone,
  statusTone,
  statusWord,
} from './words.js';
