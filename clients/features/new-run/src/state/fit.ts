/**
 * Whether a machine has the hardware a task targets, from what the helper reported of each: the
 * task's accelerator (`ExampleHardware`) and the machine's (`MachineDescription.hardware`). Only
 * the kind of accelerator and the number of GPUs are compared; a model name is never guessed at.
 */
import type {ExampleHardware, MachineDescription} from '@vibesys/contracts/host-helper';
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
 * How `machine` (called `name`) stands against `needs`: it fits when its accelerator is the kind
 * the task targets and it has enough GPUs.
 */
export function fitOf(
  needs: ExampleHardware | null,
  machine: MachineDescription | null,
  name: string,
): Fit {
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
