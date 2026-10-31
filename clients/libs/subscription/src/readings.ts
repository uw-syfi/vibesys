/**
 * A machine-stats sample as plain numbers, for the screens that draw it: no protobuf message and
 * no enum crosses into a screen. A section the host could not read is `null`, so an unreadable
 * figure can never be drawn as zero.
 */
import {AbsenceReason, type MachineStats} from '@vibesys/proto';

export interface GpuReading {
  readonly utilizationPercent: number;
  readonly usedBytes: number;
  readonly totalBytes: number;
}

/**
 * What the sample says of the GPUs: the ones it lists (none, when the tool reports none), that
 * the host has no tool to read them (so there is nothing to show), or that a read failed.
 */
export type GpuReadings =
  | {readonly kind: 'listed'; readonly gpus: readonly GpuReading[]}
  | {readonly kind: 'no-tool'}
  | {readonly kind: 'unreadable'};

export interface MachineReadings {
  readonly cpuPercent: number | null;
  readonly memory: {readonly usedBytes: number; readonly totalBytes: number} | null;
  readonly gpus: GpuReadings;
}

function gpuReadings(stats: MachineStats): GpuReadings {
  switch (stats.gpu.case) {
    case 'gpus':
      return {
        kind: 'listed',
        gpus: stats.gpu.value.gpus.map(gpu => ({
          utilizationPercent: gpu.utilizationPercent,
          usedBytes: Number(gpu.memoryUsedBytes),
          totalBytes: Number(gpu.memoryTotalBytes),
        })),
      };
    case 'gpuUnavailable':
      return stats.gpu.value.reason === AbsenceReason.TOOL_NOT_FOUND
        ? {kind: 'no-tool'}
        : {kind: 'unreadable'};
    default:
      return {kind: 'unreadable'};
  }
}

export function machineReadings(stats: MachineStats): MachineReadings {
  return {
    cpuPercent: stats.cpu.case === 'cpuStats' ? stats.cpu.value.utilizationPercent : null,
    memory:
      stats.memory.case === 'memoryStats'
        ? {
            usedBytes: Number(stats.memory.value.usedBytes),
            totalBytes: Number(stats.memory.value.totalBytes),
          }
        : null,
    gpus: gpuReadings(stats),
  };
}
