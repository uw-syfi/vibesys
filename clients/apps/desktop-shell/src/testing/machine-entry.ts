/** Valid entries as a host's gateway sends them, for the page-side fake of the subscription port. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AbsenceReason,
  MachineStatsSchema,
  RunLiveness,
  type TopicEntry,
  TopicEntrySchema,
} from '@vibesys/proto';

interface Gpu {
  readonly name: string;
  readonly utilizationPercent: number;
  readonly usedBytes: number;
  readonly totalBytes: number;
}

/** What a sample read; `null` is a section the host could not read. */
export interface Readings {
  readonly cpuPercent?: number | null;
  readonly memory?: {readonly usedBytes: number; readonly totalBytes: number} | null;
  readonly gpus?: readonly Gpu[] | null;
}

const UNREADABLE = {reason: AbsenceReason.READ_FAILED};
const GIB = 2 ** 30;

export function machineEntry(key: string, sampledMs: number, readings: Readings = {}): TopicEntry {
  const {
    cpuPercent = 12.5,
    memory = {usedBytes: 4 * GIB, totalBytes: 16 * GIB},
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
          gpus === null
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

/** A `runs` entry for `runId`: its writer `alive`, or the run `ended`. */
export function runEntry(runId: string, state: 'alive' | 'ended'): TopicEntry {
  return create(TopicEntrySchema, {
    key: runId,
    value: {
      case: 'runs',
      value: {liveness: state === 'alive' ? RunLiveness.ALIVE : RunLiveness.ENDED},
    },
  });
}
