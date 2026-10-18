import {describe, expect, test} from 'bun:test';
import type {ExampleHardware, MachineDescription} from '@vibesys/contracts/host-helper';
import {fitOf} from './fit.js';

function machine(
  accelerator: MachineDescription['hardware']['accelerator'],
  gpus: number,
): MachineDescription {
  return {
    checkout: null,
    hardware: {
      cpu_cores: 8,
      accelerator,
      gpus: Array.from({length: gpus}, () => ({vendor: 'nvidia', model: 'H100'})),
    },
    docker: {reachable: true, version: '27'},
    harness_clis: [],
    runs: {live: 0, ended_today: 0},
  };
}

const TWO_H100: ExampleHardware = {
  accelerator: 'NVIDIA H100',
  count: 2,
  vendor: 'nvidia',
  backend: 'cuda',
};

describe('whether a machine has the hardware a task targets', () => {
  test('it fits with the same kind of accelerator and enough GPUs', () => {
    expect(fitOf(TWO_H100, machine('cuda', 2), 'host-a').kind).toBe('fits');
    expect(fitOf(TWO_H100, machine('cuda', 8), 'host-a').kind).toBe('fits');
  });

  test('it does not with too few GPUs, another kind of accelerator, or none', () => {
    expect(fitOf(TWO_H100, machine('cuda', 1), 'host-a')).toEqual({
      kind: 'mismatch',
      words: 'The task targets 2× NVIDIA H100, but host-a has 1 GPU (H100).',
    });
    expect(fitOf(TWO_H100, machine('rocm', 4), 'host-a').kind).toBe('mismatch');
    expect(fitOf(TWO_H100, machine('cpu', 0), 'host-a').words).toContain('no GPU');
  });

  test('a CPU task fits any machine, and a task off the machines the helper names never does', () => {
    const cpu: ExampleHardware = {accelerator: 'CPU', count: 1, vendor: 'cpu', backend: 'cpu'};
    expect(fitOf(cpu, machine('cpu', 0), 'host-a').kind).toBe('fits');
    const trainium: ExampleHardware = {
      accelerator: 'AWS Trainium2',
      count: 1,
      vendor: 'aws',
      backend: 'trainium',
    };
    expect(fitOf(trainium, machine('cuda', 8), 'host-a').kind).toBe('mismatch');
  });

  test('nothing is claimed when either side is not known', () => {
    expect(fitOf(null, machine('cuda', 2), 'host-a').kind).toBe('unknown');
    expect(fitOf(TWO_H100, null, 'host-a').kind).toBe('unknown');
    expect(fitOf({...TWO_H100, backend: null}, machine('cuda', 2), 'host-a').kind).toBe('unknown');
  });
});
