/** The `machine_stats` topic as a thin value decoder over the shared keyed-topic lifecycle. */
import {
  type Absence,
  AbsenceReason,
  type HostService,
  type MachineStats as MachineStatsMessage,
  type TopicEntry,
  topicPolicies,
} from '@vibesys/proto';
import type * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  type AbsenceCause,
  applyMachineEvent,
  emptyMachineStats,
  type GpuReading,
  type MachineEvent,
  type MachineSample,
  type MachineStats,
  type MemoryUse,
  type Section,
} from '../state/machine-stats.js';
import type {EffectClient} from './client/make-client.js';
import type {HostConnectionApi} from './connection.js';
import {type KeyedTopicDescriptor, makeKeyedTopicWatch} from './keyed-topic-feed.js';

type HostClient = EffectClient<typeof HostService.method>;

export interface MachineStatsFeed {
  /** Mount this while the stats are on screen; unmounting closes the stream and forgets them. */
  readonly watch: Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  /** What the streams brought; a screen reads it through `machineView`. */
  readonly model: Atom.Atom<MachineStats>;
}

export interface MachineStatsParts {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly ambient: Context.Context<never>;
}

const POLICY = topicPolicies.machineStats;

/** Null for the reason the schema rejects before a frame gets here. */
const ABSENCE: Readonly<Record<AbsenceReason, AbsenceCause | null>> = {
  [AbsenceReason.UNSPECIFIED]: null,
  [AbsenceReason.TOOL_NOT_FOUND]: 'no-tool',
  [AbsenceReason.READ_FAILED]: 'read-failed',
  [AbsenceReason.TIMED_OUT]: 'timed-out',
  [AbsenceReason.PARSE_FAILED]: 'unparsed',
  [AbsenceReason.TOOL_UNVERIFIED]: 'unverified',
};

type Read<T> = Section<T> | null;

const absent = (absence: Absence): Read<never> => {
  const cause = ABSENCE[absence.reason];
  return cause === null ? null : {_tag: 'Absent', cause};
};

const bytes = (usedBytes: bigint, totalBytes: bigint): MemoryUse => ({
  usedBytes: Number(usedBytes),
  totalBytes: Number(totalBytes),
});

function cpuOf({cpu}: MachineStatsMessage): Read<number> {
  switch (cpu.case) {
    case 'cpuStats':
      return {_tag: 'Read', value: cpu.value.utilizationPercent};
    case 'cpuUnavailable':
      return absent(cpu.value);
    case undefined:
      return null;
    default:
      cpu satisfies never;
      return null;
  }
}

function memoryOf({memory}: MachineStatsMessage): Read<MemoryUse> {
  switch (memory.case) {
    case 'memoryStats':
      return {_tag: 'Read', value: bytes(memory.value.usedBytes, memory.value.totalBytes)};
    case 'memoryUnavailable':
      return absent(memory.value);
    case undefined:
      return null;
    default:
      memory satisfies never;
      return null;
  }
}

function gpusOf({gpu}: MachineStatsMessage): Read<readonly GpuReading[]> {
  switch (gpu.case) {
    case 'gpus':
      return {
        _tag: 'Read',
        value: gpu.value.gpus.map(one => ({
          index: one.index,
          name: one.name,
          utilizationPercent: one.utilizationPercent,
          memory: bytes(one.memoryUsedBytes, one.memoryTotalBytes),
        })),
      };
    case 'gpuUnavailable':
      return absent(gpu.value);
    case undefined:
      return null;
    default:
      gpu satisfies never;
      return null;
  }
}

function sampleOf(stats: MachineStatsMessage): MachineSample | null {
  const cpuPercent = cpuOf(stats);
  const memory = memoryOf(stats);
  const gpus = gpusOf(stats);
  return cpuPercent === null || memory === null || gpus === null
    ? null
    : {cpuPercent, memory, gpus};
}

function entryOf(entry: TopicEntry): {readonly event: MachineEvent} | {readonly invalid: string} {
  switch (entry.value.case) {
    case 'machineStats': {
      const sample = sampleOf(entry.value.value);
      return sample === null
        ? {invalid: 'a section with neither a reading nor a reason'}
        : {event: {_tag: 'Entry', key: entry.key, sample}};
    }
    case 'runs':
      return {invalid: 'an entry of the runs topic'};
    case undefined:
      return {invalid: 'an entry with no value'};
    default:
      entry.value satisfies never;
      return {invalid: 'an entry of an unknown topic'};
  }
}

export const makeMachineStatsFeed = Effect.fn('MachineStatsFeed.make')(function* (
  parts: MachineStatsParts,
) {
  const model = yield* SubscriptionRef.make(emptyMachineStats);
  const apply = (event: MachineEvent) =>
    SubscriptionRef.updateAndGet(model, state => applyMachineEvent(state, event, POLICY.maxKeys));
  const descriptor: KeyedTopicDescriptor<MachineStats, MachineEvent> = {
    request: {topic: {case: 'machineStats', value: {}}},
    policy: POLICY,
    open: {_tag: 'Open'},
    synced: {_tag: 'Synced'},
    aged: {_tag: 'Aged'},
    stop: {_tag: 'Stop'},
    entry: entryOf,
    removed: key => ({_tag: 'Removed', key}),
    ended: why => ({_tag: 'End', why}),
    apply,
    invalid: state => (state.ended !== null && !state.open ? state.ended : null),
  };
  const feed: MachineStatsFeed = {
    watch: makeKeyedTopicWatch(parts, descriptor),
    model: Atom.keepAlive(Atom.subscriptionRef(model)),
  };
  return feed;
});
