/**
 * Whether the place a task's results are measured has the hardware it targets, from what the
 * helper reported of each: the task's accelerator (`ExampleHardware`) and the machine's
 * (`MachineDescription.hardware`). Only the kind of accelerator and the number of GPUs are
 * compared; a model name is never guessed at. A cluster or cloud is judged by the helper's
 * `measurement_target` check, never here, and the agent's machine needs no GPU for it.
 */
import type {ExampleHardware, MachineDescription} from '@vibesys/contracts/host-helper';
import type {MeasureTarget} from '@vibesys/ipc';
import {hardwareLabel} from './catalogue.js';

export interface Fit {
  /** `unknown` when either side is not known: nothing is claimed. */
  readonly kind: 'fits' | 'mismatch' | 'unknown';
  readonly words: string;
}

/** The machine's accelerators in a few words: `2 GPUs (MI300X)`, or `no GPU`. */
export function machineHardwareWords(hardware: MachineDescription['hardware']): string {
  const {gpus} = hardware;
  if (gpus.length === 0) return 'no GPU';
  const models = [...new Set(gpus.map(gpu => gpu.model))].join(', ');
  return `${gpus.length} GPU${gpus.length === 1 ? '' : 's'} (${models})`;
}

/**
 * How the place `measure` names stands against `needs`. Measured on the agent's own machine
 * (`machine`, called `name`), it fits when its accelerator is the kind the task targets and it
 * has enough GPUs. Measured on a cluster or cloud the agent's machine is not where the work runs,
 * so nothing is claimed of it: the helper's `measurement_target` check judges the target.
 */
export function fitOf(
  needs: ExampleHardware | null,
  machine: MachineDescription | null,
  name: string,
  measure: MeasureTarget,
): Fit {
  if (measure.kind !== 'same_machine') {
    return {kind: 'unknown', words: 'The target is checked when the run is reviewed.'};
  }
  if (needs === null) {
    return {kind: 'unknown', words: 'This task does not say what hardware it targets.'};
  }
  if (machine === null) {
    return {kind: 'unknown', words: `${name} has not said what hardware it has.`};
  }
  const target = hardwareLabel(needs);
  const have = machineHardwareWords(machine.hardware);
  if (needs.backend === null) {
    return {kind: 'unknown', words: `The task targets ${target}; ${name} has ${have}.`};
  }
  if (needs.backend === 'cpu') {
    return {kind: 'fits', words: `The task runs on a CPU, which ${name} has.`};
  }
  const sameKind = machine.hardware.accelerator === needs.backend;
  const enough = needs.backend === 'metal' || machine.hardware.gpus.length >= needs.count;
  if (sameKind && enough) {
    return {kind: 'fits', words: `The task targets ${target}; ${name} has ${have}.`};
  }
  return {kind: 'mismatch', words: `The task targets ${target}, but ${name} has ${have}.`};
}
