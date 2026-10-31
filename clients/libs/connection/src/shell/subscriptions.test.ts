import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {AbsenceReason, MachineStatsSchema, type TopicEntry, TopicEntrySchema} from '@vibesys/proto';
import {ALL_TOPICS, TOPICS, type TopicArm} from '@vibesys/subscription';
import fc from 'fast-check';
import {type TopicCase, topicContract} from '../testing/topic-contract.js';

const ABSENCE = fc.constantFrom(
  AbsenceReason.TOOL_NOT_FOUND,
  AbsenceReason.READ_FAILED,
  AbsenceReason.TIMED_OUT,
  AbsenceReason.PARSE_FAILED,
  AbsenceReason.TOOL_UNVERIFIED,
);

/** A machine stats entry the schema accepts: each section a reading or the reason for none. */
const STATS_ENTRY: fc.Arbitrary<TopicEntry> = fc
  .record({
    sampledMs: fc.integer({min: 1, max: 2_000_000_000_000}),
    cpu: fc.option(fc.double({min: 0, max: 100, noNaN: true}), {nil: undefined}),
    cpuWhy: ABSENCE,
    memory: fc.option(
      fc.tuple(fc.nat({max: 2 ** 30}), fc.integer({min: 1, max: 2 ** 30})).map(([used, total]) => ({
        used: BigInt(Math.min(used, total)),
        total: BigInt(total),
      })),
      {nil: undefined},
    ),
    memoryWhy: ABSENCE,
    gpus: fc.option(
      fc.array(
        fc.record({
          utilization: fc.double({min: 0, max: 100, noNaN: true}),
          total: fc.integer({min: 1, max: 2 ** 30}),
          name: fc.constantFrom('GPU 0', 'GPU 1'),
        }),
        {maxLength: 4},
      ),
      {nil: undefined},
    ),
    gpuWhy: ABSENCE,
  })
  .map(({sampledMs, cpu, cpuWhy, memory, memoryWhy, gpus, gpuWhy}) =>
    create(TopicEntrySchema, {
      key: 'machine',
      value: {
        case: 'machineStats',
        value: create(MachineStatsSchema, {
          sampledAt: timestampFromMs(sampledMs),
          cpu:
            cpu === undefined
              ? {case: 'cpuUnavailable', value: {reason: cpuWhy}}
              : {case: 'cpuStats', value: {utilizationPercent: cpu}},
          memory:
            memory === undefined
              ? {case: 'memoryUnavailable', value: {reason: memoryWhy}}
              : {case: 'memoryStats', value: {usedBytes: memory.used, totalBytes: memory.total}},
          gpu:
            gpus === undefined
              ? {case: 'gpuUnavailable', value: {reason: gpuWhy}}
              : {
                  case: 'gpus',
                  value: {
                    gpus: gpus.map((gpu, index) => ({
                      index,
                      name: gpu.name,
                      utilizationPercent: gpu.utilization,
                      memoryUsedBytes: 0n,
                      memoryTotalBytes: BigInt(gpu.total),
                    })),
                  },
                },
        }),
      },
    }),
  );

/** One case per topic arm: a new arm in the registry fails to compile here until it has one. */
const CASES = {
  machineStats: {
    topic: TOPICS.machineStats,
    scenario: fc.record({
      first: fc.array(STATS_ENTRY, {minLength: 1, maxLength: 1}),
      changes: fc.array(STATS_ENTRY, {maxLength: 4}),
      second: fc.array(STATS_ENTRY, {minLength: 1, maxLength: 1}),
    }),
  },
} satisfies Record<TopicArm, TopicCase<unknown>>;

test('every registered topic has a contract case, and no case names an unregistered topic', () => {
  expect(ALL_TOPICS.map(topic => String(topic.arm)).sort()).toEqual(Object.keys(CASES).sort());
});

for (const each of Object.values(CASES)) topicContract(each);
