/** Typed store.v1 fixtures, consumed by projection, machine and built-app journeys. */
import {create, type DescFile, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack, FileDescriptorSetSchema} from '@bufbuild/protobuf/wkt';
import {
  AdoptionAttrsSchema,
  AgentTurnAttrsSchema,
  API_SCHEMA_HASH,
  BaselineAttrsSchema,
  CapacityWaitAttrsSchema,
  Change,
  ComparisonAttrsSchema,
  Direction,
  EpochStartedSchema,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  Outcome,
  RecordSchema,
  RuntimeType,
  SeriesRole,
  SpanStatus,
  STORE_SCHEMA_HASH,
  type Record as StoreRecord,
  StreamKind,
  StreamState,
  WorkstreamAttrsSchema,
} from '@vibesys/proto';
import {storeFacts} from './store-facts.js';

const OBJECTIVE = 'Throughput';
const HASH = '960bcc4e48b929b89a54da1613c577f938e27adffd9fefc84b176a081eba5ae6';
const LATER_OBJECTIVE = 'Steady throughput';
const LATER_HASH = '4a657cf6c298504dbf4d95c3ff7258dfef96c97ffe27063c03763eea40d7b734';

export interface StoreRunFixture {
  readonly records: readonly StoreRecord[];
  readonly initialCount: number;
  readonly blobs: Readonly<Record<string, Uint8Array>>;
  readonly streams: Readonly<Record<string, Uint8Array>>;
}
function descriptors() {
  const files = new Map<string, DescFile>();
  const add = (file: DescFile): void => {
    if (files.has(file.name)) return;
    for (const dependency of file.dependencies) add(dependency);
    files.set(file.name, file);
  };
  add(WorkstreamAttrsSchema.file);
  add(AgentTurnAttrsSchema.file);
  add(CapacityWaitAttrsSchema.file);
  return create(FileDescriptorSetSchema, {file: [...files.values()].map(file => file.proto)});
}

type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;

function epochBody(epoch = 1, objective = OBJECTIVE, hash = HASH): Body {
  return {
    case: 'epochStarted',
    value: create(EpochStartedSchema, {
      runId: 'run-a',
      epoch,
      treeHash: 'a'.repeat(40),
      vibesysVersion: 'test',
      apiSchemaHash: API_SCHEMA_HASH,
      storeSchemaHash: STORE_SCHEMA_HASH,
      descriptors: descriptors(),
      run: {
        strategy: 'search',
        input: 'input',
        objective: {sha256: hash, size: BigInt(objective.length), mediaType: 'text/plain'},
        objectives: [{series: 'throughput', direction: Direction.MAX}],
        expectedRoles: ['implementer'],
      },
      series: [
        {name: 'throughput', unit: 'ops/s', direction: Direction.MAX, role: SeriesRole.OBJECTIVE},
      ],
      spanKinds: [
        WorkstreamAttrsSchema,
        AgentTurnAttrsSchema,
        GateAttrsSchema,
        ComparisonAttrsSchema,
        BaselineAttrsSchema,
        CapacityWaitAttrsSchema,
        AdoptionAttrsSchema,
      ].map((schema, index) => ({
        kind:
          [
            'workstream',
            'agent_turn',
            'gate',
            'comparison',
            'baseline:reference',
            'capacity_wait',
            'adoption',
          ][index] ?? '',
        attrsType: schema.typeName,
        timeline: true,
        linkRels: ['candidate', 'base', 'builds_on'],
        verdicts: ['passed', 'failed_check', 'adopted'],
      })),
    }),
  };
}

function turnBody(index: number, seed: number, id: string, startedAt: number): Body {
  return {
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: `turn-${index}`,
        parent: id,
        runtimeType: RuntimeType.OPERATION,
        name: 'Implement',
        status: SpanStatus.DONE,
        startedAt: startedAt + 1,
        endedAt: startedAt + 2,
        label: {
          kind: 'agent_turn',
          title: 'Implement',
          attrs: anyPack(
            AgentTurnAttrsSchema,
            create(AgentTurnAttrsSchema, {
              agent: 'agent-a',
              role: 'implementer',
              stage: 'implement',
              provider: 'codex',
              model: 'model-a',
              startedAt: {seconds: 1700000000n + BigInt(startedAt + 1)},
              endedAt: {seconds: 1700000000n + BigInt(startedAt + 2)},
              usage: {input: BigInt(10 + seed), output: BigInt(5 + index)},
            }),
          ),
        },
      },
    },
  };
}

function gateBodies(index: number, seed: number, id: string, startedAt: number): Body[] {
  const bodies: Body[] = [];
  const push = (body: Body): void => {
    bodies.push(body);
  };
  const gateId = `gate-${index}`;
  push({
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: gateId,
        parent: id,
        runtimeType: RuntimeType.OPERATION,
        name: 'Benchmark',
        status: SpanStatus.DONE,
        startedAt: startedAt + 3,
        endedAt: startedAt + 4,
        label: {
          kind: 'gate',
          title: 'Benchmark',
          verdict: 'passed',
          attrs: anyPack(
            GateAttrsSchema,
            create(GateAttrsSchema, {stage: GateStage.BENCHMARK, subject: GateSubject.CANDIDATE}),
          ),
        },
      },
    },
  });
  push({
    case: 'metric',
    value: {series: 'throughput', span: gateId, value: 100 + seed + index, clock: startedAt + 4},
  });
  return bodies;
}

/** `count` and `seed` vary the history, not clocks or scheduling. */
export function storeRunFixture(
  kind: 'short' | 'live' | 'blobs' | 'facts' = 'short',
  count = 2,
  seed = 1,
): StoreRunFixture {
  const records: StoreRecord[] = [];
  let epoch = 1;
  const push = (body: NonNullable<MessageInitShape<typeof RecordSchema>['body']>): void => {
    records.push(
      create(RecordSchema, {
        seq: BigInt(records.length + 1),
        epoch,
        at: {seconds: 1700000000n + BigInt(records.length)},
        body,
      }),
    );
  };
  push(epochBody());
  push({case: 'lifecycle', value: {state: {case: 'running', value: {}}}});
  for (let index = 0; index < count; index += 1) {
    const id = `idea-${index}`;
    const startedAt = index * 10;
    const label = {
      kind: 'workstream',
      title: `Idea ${index + 1}`,
      verdict: 'passed',
      attrs: anyPack(
        WorkstreamAttrsSchema,
        create(WorkstreamAttrsSchema, {
          hypothesisId: id,
          idea: `Idea ${index + 1}`,
          revision: `revision-${index}`,
        }),
      ),
    };
    push({
      case: 'span',
      value: {
        change: Change.STARTED,
        span: {
          id,
          runtimeType: RuntimeType.FLOW,
          name: 'Work',
          status: SpanStatus.RUNNING,
          startedAt,
          label: {...label, verdict: undefined},
        },
      },
    });
    if (kind === 'live' && index === count - 1) break;
    push(turnBody(index, seed, id, startedAt));
    for (const body of gateBodies(index, seed, id, startedAt)) push(body);
    push({
      case: 'span',
      value: {
        change: Change.ENDED,
        span: {
          id,
          runtimeType: RuntimeType.FLOW,
          name: 'Work',
          status: SpanStatus.DONE,
          startedAt,
          endedAt: startedAt + 5,
          label,
        },
      },
    });
  }
  if (kind === 'blobs') {
    epoch = 2;
    push(epochBody(epoch, LATER_OBJECTIVE, LATER_HASH));
  }
  if (kind === 'facts') for (const body of storeFacts()) push(body);
  push({
    case: 'stream',
    value: {id: 'build-log', kind: StreamKind.LOG, title: 'Build', state: StreamState.OPENED},
  });
  if (kind !== 'live')
    push({
      case: 'lifecycle',
      value: {
        state: {case: 'ended', value: {outcome: Outcome.FINISHED, summary: '', resumable: false}},
      },
    });
  return {
    records,
    initialCount: kind === 'live' ? 3 : records.length,
    blobs: {
      [HASH]: Uint8Array.from(OBJECTIVE, char => char.charCodeAt(0)),
      ...(kind === 'blobs'
        ? {[LATER_HASH]: Uint8Array.from(LATER_OBJECTIVE, char => char.charCodeAt(0))}
        : {}),
    },
    streams: {'build-log': Uint8Array.from('Build complete\n', char => char.charCodeAt(0))},
  };
}
