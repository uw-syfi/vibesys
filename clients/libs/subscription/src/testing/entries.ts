/** Valid machine stats as a host's gateway sends them, for tests that feed a topic. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {AbsenceReason, MachineStatsSchema, type TopicEntry, TopicEntrySchema} from '@vibesys/proto';

export interface GpuSample {
  readonly name: string;
  readonly utilizationPercent: number;
  readonly usedBytes: number;
  readonly totalBytes: number;
}

/** What a sample read; `null` is a section the host could not read. The default is a quiet host. */
export interface Readings {
  readonly cpuPercent?: number | null;
  readonly memory?: {readonly usedBytes: number; readonly totalBytes: number} | null;
  /** `null`: a read that failed; `'no-tool'`: the host has no tool that reports GPUs. */
  readonly gpus?: readonly GpuSample[] | null | 'no-tool';
}

const UNREADABLE = {reason: AbsenceReason.READ_FAILED};

/** A machine-stats entry for `key`, sampled at `sampledMs` (the host's clock, display only). */
export function machineEntry(key: string, sampledMs: number, readings: Readings = {}): TopicEntry {
  const {
    cpuPercent = 12.5,
    memory = {usedBytes: 4 * 2 ** 30, totalBytes: 16 * 2 ** 30},
    gpus = [],
  } = readings;
  return create(TopicEntrySchema, {
    key,
    value: {
      case: 'machineStats',
      value: create(MachineStatsSchema, {
        sampledAt: timestampFromMs(sampledMs),
        cpu:
          cpuPercent === null
            ? {case: 'cpuUnavailable', value: UNREADABLE}
            : {case: 'cpuStats', value: {utilizationPercent: cpuPercent}},
        memory:
          memory === null
            ? {case: 'memoryUnavailable', value: UNREADABLE}
            : {
                case: 'memoryStats',
                value: {usedBytes: BigInt(memory.usedBytes), totalBytes: BigInt(memory.totalBytes)},
              },
        gpu:
          gpus === 'no-tool'
            ? {case: 'gpuUnavailable', value: {reason: AbsenceReason.TOOL_NOT_FOUND}}
            : gpus === null
              ? {case: 'gpuUnavailable', value: UNREADABLE}
              : {
                  case: 'gpus',
                  value: {
                    gpus: gpus.map((gpu, index) => ({
                      index,
                      name: gpu.name,
                      utilizationPercent: gpu.utilizationPercent,
                      memoryUsedBytes: BigInt(gpu.usedBytes),
                      memoryTotalBytes: BigInt(gpu.totalBytes),
                    })),
                  },
                },
      }),
    },
  });
}
