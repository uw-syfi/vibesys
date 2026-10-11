/**
 * Test support for control-state machines: the model checker over seeded event sequences.
 * Reachable only from test files. Drive a live actor's delays with a fake clock through
 * `clockFrom` (the package's `.` entry) and `createActor`'s `clock` option.
 */
export {checkMachine, type Live, type MachineAction, type MachineModel} from './check-machine.js';
