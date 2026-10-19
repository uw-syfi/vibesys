import {describe, expect, test} from 'bun:test';
import type {ExampleHardware, MachineDescription} from '@vibesys/contracts/host-helper';
import type {MeasureTarget} from '@vibesys/ipc';
import {fitOf} from './fit.js';

const SAME: MeasureTarget = {kind: 'same_machine'};
const SLURM: MeasureTarget = {kind: 'slurm', cluster: 'cluster-m'};
const MODAL: MeasureTarget = {kind: 'modal'};

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
    expect(fitOf(TWO_H100, machine('cuda', 2), 'host-a', SAME).kind).toBe('fits');
    expect(fitOf(TWO_H100, machine('cuda', 8), 'host-a', SAME).kind).toBe('fits');
  });

  test('it does not with too few GPUs, another kind of accelerator, or none', () => {
    expect(fitOf(TWO_H100, machine('cuda', 1), 'host-a', SAME)).toEqual({
      kind: 'mismatch',
      words: 'The task targets 2× NVIDIA H100, but host-a has 1 GPU (H100).',
    });
    expect(fitOf(TWO_H100, machine('rocm', 4), 'host-a', SAME).kind).toBe('mismatch');
    expect(fitOf(TWO_H100, machine('cpu', 0), 'host-a', SAME).words).toContain('no GPU');
  });

  test('a CPU task fits any machine, and a task off the machines the helper names never does', () => {
    const cpu: ExampleHardware = {accelerator: 'CPU', count: 1, vendor: 'cpu', backend: 'cpu'};
    expect(fitOf(cpu, machine('cpu', 0), 'host-a', SAME).kind).toBe('fits');
    const trainium: ExampleHardware = {
      accelerator: 'AWS Trainium2',
      count: 1,
      vendor: 'aws',
      backend: 'trainium',
    };
    expect(fitOf(trainium, machine('cuda', 8), 'host-a', SAME).kind).toBe('mismatch');
  });

  test('nothing is claimed when either side is not known', () => {
    expect(fitOf(null, machine('cuda', 2), 'host-a', SAME).kind).toBe('unknown');
    expect(fitOf(TWO_H100, null, 'host-a', SAME).kind).toBe('unknown');
    expect(fitOf({...TWO_H100, backend: null}, machine('cuda', 2), 'host-a', SAME).kind).toBe(
      'unknown',
    );
  });
});

describe('where the results are measured decides what is judged', () => {
  const backends = ['cuda', 'rocm', 'metal', 'cpu', 'trainium', null] as const;
  const machines = [machine('cpu', 0), machine('cuda', 8), machine('rocm', 4), null];
  const remote = [SLURM, MODAL, {kind: 'slurm', cluster: 'cluster-n'} as const];

  test('the agent machine never misfits a task measured on a cluster or cloud', () => {
    for (const backend of backends) {
      for (const agent of machines) {
        for (const measure of remote) {
          const needs: ExampleHardware = {...TWO_H100, backend};
          const fit = fitOf(needs, agent, 'host-a', measure);
          expect(fit.kind).toBe('unknown');
          expect(fit.words).not.toContain('no GPU');
          expect(fit.words).not.toContain('host-a');
        }
      }
    }
  });

  test('a GPU task on a CPU-only machine misfits only when measured on that machine', () => {
    const cpuOnly = machine('cpu', 0);
    expect(fitOf(TWO_H100, cpuOnly, 'host-a', SAME).kind).toBe('mismatch');
    expect(fitOf(TWO_H100, cpuOnly, 'host-a', SLURM).kind).toBe('unknown');
    expect(fitOf(TWO_H100, cpuOnly, 'host-a', MODAL).kind).toBe('unknown');
  });
});
