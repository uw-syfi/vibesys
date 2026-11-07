/**
 * The host's `machine_stats` topic as a recording serves it: `Subscribe` frames built from plain
 * figures, and a wrap that answers the topic's calls with them and leaves every other call to the
 * recording it wraps.
 */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type AbsenceReason,
  HostService,
  type MachineStatsSchema,
  SubscribeResponseSchema,
  type TopicFailureCode,
} from '@vibesys/proto';
import type {Recording, Step} from './transport/index.js';

export const SUBSCRIBE = `/${HostService.typeName}/${HostService.method.subscribe.name}`;

/** The key the host's one value is under. */
const MACHINE_KEY = 'machine';

/** A section's figures, or the reason the host has none. */
type Figure<T> = T | {readonly absent: AbsenceReason};

interface GpuFigures {
  readonly name: string;
  readonly utilizationPercent: number;
  readonly memoryUsedBytes: bigint;
  readonly memoryTotalBytes: bigint;
}

export interface MachineFigures {
  readonly cpuPercent: Figure<number>;
  readonly memory: Figure<{readonly usedBytes: bigint; readonly totalBytes: bigint}>;
  /** An empty list is a machine with no GPU. */
  readonly gpus: Figure<readonly GpuFigures[]>;
}

const GIB = 1024n ** 3n;

/** A machine with two GPUs, each section read. */
export const TWO_GPUS: MachineFigures = {
  cpuPercent: 37.5,
  memory: {usedBytes: 41n * GIB, totalBytes: 128n * GIB},
  gpus: [
    {
      name: 'NVIDIA H100 80GB HBM3',
      utilizationPercent: 92,
      memoryUsedBytes: 71n * GIB,
      memoryTotalBytes: 80n * GIB,
    },
    {
      name: 'NVIDIA H100 80GB HBM3',
      utilizationPercent: 0,
      memoryUsedBytes: 0n,
      memoryTotalBytes: 80n * GIB,
    },
  ],
};

type StatsInit = Required<MessageInitShape<typeof MachineStatsSchema>>;
type FrameInit = NonNullable<MessageInitShape<typeof SubscribeResponseSchema>['frame']>;

const absence = (figure: {readonly absent: AbsenceReason}) => ({reason: figure.absent});

const cpuOf = (cpu: Figure<number>): StatsInit['cpu'] =>
  typeof cpu === 'number'
    ? {case: 'cpuStats', value: {utilizationPercent: cpu}}
    : {case: 'cpuUnavailable', value: absence(cpu)};

const memoryOf = (memory: MachineFigures['memory']): StatsInit['memory'] =>
  'absent' in memory
    ? {case: 'memoryUnavailable', value: absence(memory)}
    : {case: 'memoryStats', value: memory};

const gpusOf = (gpus: MachineFigures['gpus']): StatsInit['gpu'] =>
  'absent' in gpus
    ? {case: 'gpuUnavailable', value: absence(gpus)}
    : {case: 'gpus', value: {gpus: gpus.map((gpu, index) => ({index, ...gpu}))}};

const frame = (init: FrameInit): Step => ({
  frame: toBinary(SubscribeResponseSchema, create(SubscribeResponseSchema, {frame: init})),
});

/** The key holds a sample of `figures`, sampled at `sampledAtMs` by the host's clock. */
export const statsFrame = (
  figures: MachineFigures,
  options: {readonly key?: string; readonly sampledAtMs?: number} = {},
): Step =>
  frame({
    case: 'entry',
    value: {
      key: options.key ?? MACHINE_KEY,
      value: {
        case: 'machineStats',
        value: {
          sampledAt: timestampFromMs(options.sampledAtMs ?? 0),
          cpu: cpuOf(figures.cpuPercent),
          memory: memoryOf(figures.memory),
          gpu: gpusOf(figures.gpus),
        },
      },
    },
  });

export const SYNCED_FRAME: Step = frame({case: 'synced', value: {}});
export const HEARTBEAT_FRAME: Step = frame({case: 'heartbeat', value: {}});
/** An entry of the runs topic, which a stream of machine stats never carries. */
export const RUNS_ENTRY_FRAME: Step = frame({
  case: 'entry',
  value: {key: 'run-1', value: {case: 'runs', value: {}}},
});

export const refusedFrame = (code: TopicFailureCode): Step =>
  frame({case: 'failed', value: {code, detail: 'refused'}});

/**
 * A stream that keeps its cadence: the snapshot, then for each of `values` a heartbeat half a
 * period in and the next value at the period, then nothing more until it is given up on.
 */
export const steadyStream = (
  figures: (nth: number) => MachineFigures,
  values: number,
  periodMs = 10_000,
): Step[] => {
  const steps: Step[] = [statsFrame(figures(0)), SYNCED_FRAME];
  for (let nth = 1; nth <= values; nth += 1) {
    steps.push({sleep: periodMs / 2}, HEARTBEAT_FRAME, {sleep: periodMs / 2});
    steps.push(statsFrame(figures(nth)));
  }
  return [...steps, {hang: true}];
};

/** `base`, with its `nth` call to the topic (from 0) answered by `streams(nth)`. */
export const servingMachineStats =
  (streams: (nth: number) => ReadonlyArray<Step>) =>
  (base: Recording): Recording => ({
    respond: (target, nth) =>
      target.method === SUBSCRIBE ? streams(nth) : base.respond(target, nth),
  });
