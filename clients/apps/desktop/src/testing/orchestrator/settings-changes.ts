/**
 * Builders and generators for a run's settings changes (`run_updated`), shared by the fold's property
 * test and the screens' tests, so no test writes the wide message by hand.
 */
import {create} from '@bufbuild/protobuf';
import {DurationSchema, timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AgentSpecSchema,
  CostLimitChangeSchema,
  FanoutChangeSchema,
  Harness,
  LeadChangeSchema,
  LeadChangeState,
  type RunChange,
  RunChangeSchema,
  RunUpdatedSchema,
  type SettingChange,
  SettingChangeSchema,
  TimeLimitChangeSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';

const duration = (seconds: number | null) =>
  seconds === null ? undefined : create(DurationSchema, {seconds: BigInt(seconds)});

function costChange(from: number | null, to: number | null): SettingChange {
  return create(SettingChangeSchema, {
    change: {
      case: 'costLimit',
      value: create(CostLimitChangeSchema, {
        ...(from === null ? {} : {from}),
        ...(to === null ? {} : {to}),
      }),
    },
  });
}

function timeChange(from: number | null, to: number | null): SettingChange {
  return create(SettingChangeSchema, {
    change: {
      case: 'timeLimit',
      value: create(TimeLimitChangeSchema, {from: duration(from), to: duration(to)}),
    },
  });
}

function fanoutChange(from: number, to: number): SettingChange {
  return create(SettingChangeSchema, {
    change: {case: 'fanout', value: create(FanoutChangeSchema, {from, to})},
  });
}

function leadChange(
  from: {harness: Harness; model: string} | null,
  to: {harness: Harness; model: string},
  state: 'pending' | 'applied',
): SettingChange {
  return create(SettingChangeSchema, {
    change: {
      case: 'lead',
      value: create(LeadChangeSchema, {
        ...(from === null ? {} : {from: create(AgentSpecSchema, from)}),
        to: create(AgentSpecSchema, to),
        state: state === 'pending' ? LeadChangeState.PENDING : LeadChangeState.APPLIED,
      }),
    },
  });
}

/** One committed `run_updated` of `runId`, at `sequence` and `atMs`. */
function runUpdatedChange(
  runId: string,
  sequence: number,
  atMs: number,
  changes: readonly SettingChange[],
): RunChange {
  return create(RunChangeSchema, {
    sequence: BigInt(sequence),
    at: timestampFromMs(atMs),
    runId,
    body: {case: 'runUpdated', value: create(RunUpdatedSchema, {runId, changes: [...changes]})},
  });
}

/** Any one setting change a host can report. */
const settingChanges: fc.Arbitrary<SettingChange> = fc.oneof(
  fc
    .tuple(
      fc.option(fc.integer({min: 1, max: 500}), {nil: null}),
      fc.option(fc.integer({min: 1, max: 500}), {nil: null}),
    )
    .map(([from, to]) => costChange(from, to)),
  fc
    .tuple(
      fc.option(fc.integer({min: 60, max: 90_000}), {nil: null}),
      fc.option(fc.integer({min: 60, max: 90_000}), {nil: null}),
    )
    .map(([from, to]) => timeChange(from, to)),
  fc
    .tuple(fc.integer({min: 1, max: 8}), fc.integer({min: 1, max: 8}))
    .map(([a, b]) => fanoutChange(a, b)),
  fc
    .tuple(
      fc.constantFrom(Harness.CLAUDE_CODE, Harness.CODEX),
      fc.constantFrom('', 'model-a', 'model-b'),
      fc.constantFrom<'pending' | 'applied'>('pending', 'applied'),
    )
    .map(([harness, model, state]) => leadChange(null, {harness, model}, state)),
);

/** A stream of `run_updated` changes of a few runs, in sequence from 1, a second apart. */
export const settingsStreams: fc.Arbitrary<RunChange[]> = fc
  .array(
    fc.tuple(
      fc.constantFrom('run-a', 'run-b', 'run-c'),
      fc.array(settingChanges, {minLength: 1, maxLength: 4}),
    ),
    {maxLength: 30},
  )
  .map(updates =>
    updates.map(([runId, changes], index) =>
      runUpdatedChange(runId, index + 1, 1_000_000 + index * 1000, changes),
    ),
  );
