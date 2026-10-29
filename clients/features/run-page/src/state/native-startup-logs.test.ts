import {expect, test} from 'bun:test';
import {create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  DrainGoal,
  KeyframeSchema,
  LifecycleSchema,
  LogGate,
  Outcome,
  RecordSchema,
  StageState,
  StartupStage,
  type Record as StoreRecord,
  StreamKind,
  StreamOwnerSchema,
  StreamRefSchema,
  StreamState,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {readSkeleton} from '@vibesys/run-state/skeleton';
import {
  logStreamToFollow,
  projectSkeleton,
  RUN_ROUTE,
  runPageInitial,
  skeletonScreenState,
  timelineView,
} from './index.js';

import {logGroups} from './views.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
const STAGES = [
  [StartupStage.PROBING_WORKSPACE, 'probing_workspace'],
  [StartupStage.BUILDING_IMAGE, 'building_image'],
  [StartupStage.BUILDING_TOOLS, 'building_tools'],
  [StartupStage.STARTING_CONTAINER, 'starting_container'],
] as const;

function sequence(seed: number) {
  const records: StoreRecord[] = [...storeRunFixture('live', 1, seed).records.slice(0, 1)];
  const expected = new Map<
    string,
    {
      kind: string;
      stage?: string;
      name?: string;
      gate?: string;
      execution_id?: string;
      attempt?: number;
      span_id: null;
    }
  >();
  const checkpoints: {
    records: readonly StoreRecord[];
    owners: typeof expected;
    active: string | null;
  }[] = [];
  const emit = (body: StoreRecord['body']) => {
    records.push(
      create(RecordSchema, {
        seq: BigInt(records.length + 1),
        epoch: 1,
        at: {seconds: 1700000000n},
        body,
      }),
    );
  };
  const lifecycle = (state: NonNullable<MessageInitShape<typeof LifecycleSchema>['state']>) =>
    emit({case: 'lifecycle', value: create(LifecycleSchema, {state})});
  const save = (active: string | null) =>
    checkpoints.push({records: [...records], owners: new Map(expected), active});
  emit({
    case: 'stream',
    value: create(StreamRefSchema, {
      id: 'historical',
      kind: StreamKind.LOG,
      title: 'Starting the agent container',
      state: StreamState.OPENED,
    }),
  });
  expected.set('historical', {kind: 'run', span_id: null});
  const gates = [
    [LogGate.BUILD, 'build'],
    [LogGate.ACCURACY, 'accuracy'],
    [LogGate.BENCHMARK, 'benchmark'],
    [LogGate.VALIDATION, 'validation'],
  ] as const;
  const [gate, gateWord] = gates[seed % gates.length] ?? gates[0];
  for (let index = 0; index < 4 + (seed % 3); index += 1) {
    const [stage, word] = STAGES[(index + seed) % STAGES.length] ?? STAGES[0];
    lifecycle({case: 'starting', value: {stage, state: StageState.RUNNING}});
    const id = `startup-${index}`;
    emit({
      case: 'stream',
      value: create(StreamRefSchema, {
        id,
        kind: StreamKind.LOG,
        title: 'gate narration misleading title',
        state: StreamState.OPENED,
        owner: {kind: {case: 'startup', value: stage}},
      }),
    });
    expected.set(id, {kind: 'startup_step', stage: word, span_id: null});
    if (index > 0 && index % 2 === seed % 2) {
      const previousIndex = index >= STAGES.length ? index - STAGES.length : index - 1;
      const [previousStage] = STAGES[(previousIndex + seed) % STAGES.length] ?? STAGES[0];
      emit({
        case: 'stream',
        value: create(StreamRefSchema, {
          id: `startup-${previousIndex}`,
          kind: StreamKind.LOG,
          title: 'late close for an older stream',
          state: StreamState.ENDED,
          length: 0n,
          owner: {kind: {case: 'startup', value: previousStage}},
        }),
      });
    }
    save(id);
    const kinds: {
      owner: NonNullable<MessageInitShape<typeof StreamOwnerSchema>['kind']>;
      expected: typeof expected extends Map<string, infer T> ? T : never;
    }[] = [
      {
        owner: {
          case: 'gate',
          value: {execution: `eval-${seed}`, gate, attempt: seed},
        },
        expected: {
          kind: 'gate_stage',
          execution_id: `eval-${seed}`,
          gate: gateWord,
          attempt: seed,
          span_id: null,
        },
      },
      {
        owner: {case: 'toolBuild', value: `tool-${seed}`},
        expected: {kind: 'tool_build', name: `tool-${seed}`, span_id: null},
      },
      {owner: {case: 'narration', value: true}, expected: {kind: 'run_narration', span_id: null}},
    ];
    const item = kinds[(index + seed) % kinds.length];
    if (!item) throw new Error('generated owner required');
    const other = `other-${index}`;
    emit({
      case: 'stream',
      value: create(StreamRefSchema, {
        id: other,
        kind: StreamKind.LOG,
        title: 'startup misleading title',
        state: StreamState.ENDED,
        length: BigInt(seed),
        owner: create(StreamOwnerSchema, {kind: item.owner}),
      }),
    });
    expected.set(other, item.expected);
    save(id);
    emit({
      case: 'stream',
      value: create(StreamRefSchema, {
        id,
        kind: StreamKind.LOG,
        title: 'changed display title',
        state: StreamState.ENDED,
        length: 0n,
        owner: {kind: {case: 'startup', value: stage}},
      }),
    });
    lifecycle({case: 'starting', value: {stage, state: StageState.FINISHED, elapsedSeconds: seed}});
    save(null);
  }
  const endings: NonNullable<MessageInitShape<typeof LifecycleSchema>['state']>[] = [
    {case: 'running', value: {}},
    {case: 'draining', value: {goal: DrainGoal.PARK}},
    {case: 'parked', value: {}},
    {case: 'ended', value: {outcome: Outcome.FINISHED}},
  ];
  for (const state of endings) {
    lifecycle(state);
    save(null);
  }
  return checkpoints;
}

for (let seed = 1; seed <= 24; seed += 1) {
  test(`regression: native log owners and active startup following survive generated lifecycle prefixes and keyframes (${seed})`, () => {
    for (const {records, owners, active} of sequence(seed)) {
      const wire = records.map(record => fromBinary(RecordSchema, toBinary(RecordSchema, record)));
      const skeleton = foldSkeleton(seedSkeleton(), wire);
      const cached = seedSkeleton(
        fromBinary(
          KeyframeSchema,
          toBinary(
            KeyframeSchema,
            create(KeyframeSchema, {
              ...skeleton,
              throughSeq: skeleton.throughSeq ?? 0n,
              logOffset: 0n,
            }),
          ),
        ),
      );
      for (const source of [skeleton, cached]) {
        const at = source.throughSeq ?? 0n;
        const reading = readSkeleton(source, seekSkeleton(source, at));
        expect(new Map(reading.streams.map(stream => [stream.id, stream.owner]))).toEqual(owners);
        const model = projectSkeleton(source, at);
        const state = skeletonScreenState(SEED, source, model, runPageInitial(SEED));
        for (const step of timelineView(state)) {
          const latest =
            [...owners]
              .filter(([, owner]) => owner.kind === 'startup_step' && owner.stage === step.id)
              .at(-1)?.[0] ?? null;
          expect(step.streamId).toBe(latest);
        }
        expect(logStreamToFollow(state, model, RUN_ROUTE)).toBe(active);
        expect(
          logGroups(state)
            .flatMap(group => group.streams)
            .map(stream => stream.id)
            .sort(),
        ).toEqual([...owners.keys()].sort());
      }
    }
  });
}
