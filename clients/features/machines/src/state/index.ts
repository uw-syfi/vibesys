/**
 * The machines feature's pure state: the Machines screen's store (the saved machines, each one's
 * status from its facts, the Add form and the checkout form with path suggestions) and the words
 * and facts the screen shows.
 */
export {
  type FormName,
  initialMachines,
  type Machine,
  type MachineStatus,
  type MachinesAction,
  type MachinesRequest,
  type MachinesState,
  type MachinesStep,
  machineStatus,
  machinesAwaited,
  machinesInvariants,
  machinesStep,
  selectedMachine,
} from './machines.js';
export {formSuggestions} from './path-form.js';
export {
  type MachineFact,
  machineFacts,
  machineProblem,
  machineSummary,
  type StatusTone,
  statusTone,
  statusWord,
} from './words.js';
