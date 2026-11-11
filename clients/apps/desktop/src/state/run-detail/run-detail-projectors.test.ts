import {create, toBinary} from '@bufbuild/protobuf';
import {
  type Any,
  AnySchema,
  anyPack,
  FileDescriptorSetSchema,
  timestampFromMs,
} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentClient,
  AgentTurnAttrsSchema,
  BlobRefSchema,
  Change,
  EvidenceKind,
  FrameworkSource,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  RecordSchema,
  RunHeadSchema,
  RuntimeType,
  SkeletonResponseSchema,
  SpanFailureKind,
  SpanKindDeclSchema,
  SpanRecordSchema,
  SpanSchema,
  SpanStatus,
  type Record as StoreRecord,
  TailResponseSchema,
  WorkstreamAttrsSchema,
  WorkstreamFailureKind,
} from '@vibesys/proto';
import fc from 'fast-check';
import {
  emptyRunDetail,
  ingestRunDetailSkeleton,
  ingestRunDetailTail,
  projectRunDetail,
  type RunDetailState,
} from './index.js';

const RUN = 'run-projectors';
const HASH = '0'.repeat(64);
const AT = timestampFromMs(1_000);
const descriptorSet = create(FileDescriptorSetSchema, {
  file: [
    {
      name: 'agent.proto',
      package: 'vibesys.store.v1',
      syntax: 'proto3',
      messageType: [{name: 'AgentTurnAttrs'}],
    },
    {
      name: 'search.proto',
      package: 'vibesys.search.v1',
      syntax: 'proto3',
      messageType: [{name: 'WorkstreamAttrs'}, {name: 'GateAttrs'}],
    },
    {
      name: 'custom.proto',
      package: 'example.custom.v1',
      syntax: 'proto3',
      messageType: [{name: 'CustomAttrs'}],
    },
  ],
});

function header(
  sequence = 1n,
  bodyCase: 'epochStarted' | 'declarationAmended' = 'epochStarted',
  workstreamType: string = WorkstreamAttrsSchema.typeName,
  descriptors = descriptorSet,
): StoreRecord {
  const epoch = {
    runId: RUN,
    epoch: 1,
    treeHash: 'tree',
    apiSchemaHash: HASH,
    storeSchemaHash: HASH,
    vibesysVersion: 'test',
    run: {
      strategy: 'natural-language',
      input: 'base',
      objective: create(BlobRefSchema, {sha256: HASH, mediaType: 'text/plain'}),
      expectedRoles: ['planner'],
    },
    spanKinds: [
      create(SpanKindDeclSchema, {
        kind: 'workstream',
        attrsType: workstreamType,
        timeline: true,
      }),
      create(SpanKindDeclSchema, {
        kind: 'gate',
        attrsType: GateAttrsSchema.typeName,
        timeline: true,
      }),
      create(SpanKindDeclSchema, {
        kind: 'agent_turn',
        attrsType: AgentTurnAttrsSchema.typeName,
        timeline: true,
      }),
      create(SpanKindDeclSchema, {
        kind: 'custom',
        attrsType: 'example.custom.v1.CustomAttrs',
        timeline: true,
      }),
    ],
    descriptors,
  };
  return create(RecordSchema, {
    seq: sequence,
    epoch: 1,
    at: AT,
    body:
      bodyCase === 'epochStarted'
        ? {case: 'epochStarted', value: epoch}
        : {case: 'declarationAmended', value: epoch},
  });
}

interface SpanOptions {
  readonly sequence: bigint;
  readonly id: string;
  readonly kind: string;
  readonly attrs: Any;
  readonly change?: Change;
  readonly status?: SpanStatus;
  readonly parent?: string;
  readonly title?: string;
  readonly verdict?: string;
  readonly reason?: string;
  readonly links?: readonly {readonly rel: string; readonly to: string}[];
  readonly failed?: boolean;
}

function spanRecord(options: SpanOptions): StoreRecord {
  const status = options.status ?? SpanStatus.RUNNING;
  const terminal = status >= SpanStatus.DONE;
  return create(RecordSchema, {
    seq: options.sequence,
    epoch: 1,
    at: AT,
    body: {
      case: 'span',
      value: create(SpanRecordSchema, {
        change: options.change ?? Change.STARTED,
        span: create(SpanSchema, {
          id: options.id,
          parent: options.parent,
          runtimeType: RuntimeType.OPERATION,
          name: options.kind,
          status,
          startedAt: 1,
          endedAt: terminal ? Number(options.sequence) : undefined,
          failure: options.failed
            ? {kind: SpanFailureKind.OUTCOME, message: 'recorded failure'}
            : undefined,
          label: {
            kind: options.kind,
            title: options.title ?? options.id,
            verdict: options.verdict,
            reason: options.reason,
            links: [...(options.links ?? [])],
            attrs: options.attrs,
          },
        }),
      }),
    },
  });
}

function workstreamAttrs(overrides: Record<string, unknown> = {}): Any {
  return anyPack(
    WorkstreamAttrsSchema,
    create(WorkstreamAttrsSchema, {
      hypothesisId: 'hypothesis-a',
      parentRevision: 'revision-base',
      idea: 'Try the typed path',
      sequence: 1,
      ...overrides,
    }),
  );
}

function gateAttrs(): Any {
  return anyPack(
    GateAttrsSchema,
    create(GateAttrsSchema, {
      stage: GateStage.MEASUREMENT,
      subject: GateSubject.CANDIDATE,
      recipe: 'trusted',
      command: 'evaluate',
      reused: true,
      failed: {
        check: EvidenceKind.CORRECTNESS,
        completed: 93n,
        required: 100n,
        unit: 'trials',
      },
      source: FrameworkSource.GATES,
      sourceLabel: 'evaluation',
      stages: ['accuracy', 'benchmark'],
    }),
  );
}

function agentAttrs(role: string): Any {
  return anyPack(
    AgentTurnAttrsSchema,
    create(AgentTurnAttrsSchema, {
      agent: 'agent:dynamic',
      role,
      stage: 'implementation',
      startedBy: ['orchestrator'],
      client: AgentClient.PROVIDER,
    }),
  );
}

function snapshot(records: readonly StoreRecord[], committedSequence = records.at(-1)?.seq ?? 0n) {
  const through = records.at(-1)?.seq;
  return create(SkeletonResponseSchema, {
    records: records.map(record => toBinary(RecordSchema, record)),
    committedSeq: committedSequence,
    nextSeq: through === undefined ? 1n : through + 1n,
    complete: (through ?? 0n) === committedSequence,
    head: create(RunHeadSchema, {
      strategy: 'natural-language',
      committedSeq: committedSequence,
      epoch: 1,
    }),
  });
}

function tail(records: readonly StoreRecord[]) {
  return create(TailResponseSchema, {
    records: records.map(record => toBinary(RecordSchema, record)),
    throughSeq: records.at(-1)?.seq ?? 0n,
    head: create(RunHeadSchema, {
      strategy: 'natural-language',
      committedSeq: records.at(-1)?.seq ?? 0n,
      epoch: 1,
    }),
  });
}

function applied(update: ReturnType<typeof ingestRunDetailSkeleton>): RunDetailState {
  if (update.kind !== 'applied') {
    const detail = 'reason' in update ? update.reason : update.kind;
    throw new Error(`snapshot rejected: ${detail}`);
  }
  return update.state;
}

describe('typed run-detail span projectors', () => {
  it('projects workstream lineage, correctness evidence, updates, and an open agent role', () => {
    const role = 'bespoke-systems-critic';
    const records = [
      header(),
      spanRecord({
        sequence: 2n,
        id: 'work-root',
        kind: 'workstream',
        attrs: workstreamAttrs({hypothesisId: 'root', sequence: 1}),
      }),
      spanRecord({
        sequence: 3n,
        id: 'work-child',
        parent: 'flow-child',
        kind: 'workstream',
        attrs: workstreamAttrs({hypothesisId: 'child', sequence: 2}),
        links: [{rel: 'builds_on', to: 'work-root'}],
      }),
      header(4n, 'declarationAmended', GateAttrsSchema.typeName),
      spanRecord({
        sequence: 5n,
        id: 'work-child',
        parent: 'flow-child',
        kind: 'workstream',
        change: Change.ENDED,
        status: SpanStatus.FAILED,
        verdict: 'failed',
        reason: 'turned_down',
        failed: true,
        links: [{rel: 'builds_on', to: 'work-root'}],
        attrs: workstreamAttrs({
          hypothesisId: 'child',
          sequence: 2,
          revision: 'revision-child',
          outcome: 'reviewed',
          summary: 'Typed summary',
          attempts: 2,
          profileSkipped: false,
          judgeVerdict: 'reject',
          repeatedError: true,
          failure: {
            kind: WorkstreamFailureKind.REVIEW_REJECTED,
            role,
            turns: 3,
            limit: 4,
            text: 'review rejected the candidate',
          },
          files: [{path: 'src/main.ts', diffOffset: 10n, diffLength: 20n}],
        }),
      }),
      spanRecord({
        sequence: 6n,
        id: 'gate-child',
        parent: 'work-child',
        kind: 'gate',
        attrs: gateAttrs(),
      }),
      spanRecord({
        sequence: 7n,
        id: 'gate-child',
        parent: 'work-child',
        kind: 'gate',
        change: Change.ENDED,
        attrs: gateAttrs(),
        status: SpanStatus.FAILED,
        verdict: 'failed_check',
        reason: 'accuracy',
        failed: true,
        links: [{rel: 'candidate', to: 'work-child'}],
      }),
      spanRecord({
        sequence: 8n,
        id: 'agent-dynamic',
        parent: 'work-child',
        kind: 'agent_turn',
        attrs: agentAttrs(role),
      }),
    ];
    const view = projectRunDetail(
      applied(ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records))),
    );

    expect(view.workstreams).toMatchObject({
      availability: {kind: 'available'},
      freshness: {kind: 'current', throughSequence: 8n},
      scope: {kind: 'run', runId: RUN},
    });
    expect(view.workstreams.values).toHaveLength(2);
    expect(view.workstreams.values[1]).toMatchObject({
      spanId: 'work-child',
      parentSpanId: 'flow-child',
      lifecycle: 'failed',
      verdict: 'failed',
      reason: 'turned_down',
      links: [{rel: 'builds_on', to: 'work-root'}],
      hypothesisId: 'child',
      revision: 'revision-child',
      sequence: 2,
      repeatedError: true,
      source: {sequence: 5n, epoch: 1},
      scope: {kind: 'span', runId: RUN, spanId: 'work-child'},
      spanFailure: {kind: 'outcome', message: 'recorded failure'},
      workstreamFailure: {
        kind: 'review-rejected',
        role,
        turns: 3,
        limit: 4,
        text: 'review rejected the candidate',
      },
      files: [{path: 'src/main.ts', diffOffset: 10n, diffLength: 20n}],
    });
    expect(view.correctness).toMatchObject({
      availability: {kind: 'available'},
      values: [
        {
          spanId: 'gate-child',
          parentSpanId: 'work-child',
          lifecycle: 'failed',
          verdict: 'failed_check',
          reason: 'accuracy',
          links: [{rel: 'candidate', to: 'work-child'}],
          stage: 'measurement',
          subject: 'candidate',
          failedCheck: {check: 'correctness', completed: 93n, required: 100n, unit: 'trials'},
          frameworkSource: 'gates',
          stages: ['accuracy', 'benchmark'],
          source: {sequence: 7n, epoch: 1},
          scope: {kind: 'span', runId: RUN, spanId: 'gate-child'},
        },
      ],
    });
    expect(view.agents.values).toMatchObject([{role, source: {sequence: 8n}}]);
    expect(view.unprojectedSpans.values).toEqual([]);
  });

  it('keeps descriptor, validation, and unsupported projector failures explicit', () => {
    const missingSearchDescriptors = create(FileDescriptorSetSchema, {
      file: descriptorSet.file.filter(file => file.package !== 'vibesys.search.v1'),
    });
    const unknown = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN),
          snapshot([
            header(1n, 'epochStarted', WorkstreamAttrsSchema.typeName, missingSearchDescriptors),
            spanRecord({
              sequence: 2n,
              id: 'unknown-descriptor',
              kind: 'workstream',
              attrs: workstreamAttrs(),
            }),
          ]),
        ),
      ),
    );
    expect(unknown.workstreams).toMatchObject({
      availability: {kind: 'partial', reasons: ['descriptor-unknown']},
      values: [],
    });
    expect(unknown.unprojectedSpans.values).toMatchObject([
      {spanId: 'unknown-descriptor', reason: 'descriptor-unknown'},
    ]);

    const mismatch = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN),
          snapshot([
            header(),
            spanRecord({sequence: 2n, id: 'mismatch', kind: 'workstream', attrs: gateAttrs()}),
          ]),
        ),
      ),
    );
    expect(mismatch.workstreams.availability).toEqual({
      kind: 'partial',
      reasons: ['invalid-typed-fact'],
    });
    expect(mismatch.correctness.availability).toEqual({kind: 'available'});
    expect(mismatch.unprojectedSpans.values).toMatchObject([
      {
        spanId: 'mismatch',
        attrsType: GateAttrsSchema.typeName,
        declaredAttrsType: WorkstreamAttrsSchema.typeName,
        reason: 'descriptor-mismatch',
      },
    ]);

    const invalid = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN),
          snapshot([
            header(),
            spanRecord({
              sequence: 2n,
              id: 'invalid',
              kind: 'workstream',
              attrs: workstreamAttrs({hypothesisId: ''}),
            }),
          ]),
        ),
      ),
    );
    expect(invalid.workstreams.availability).toEqual({
      kind: 'partial',
      reasons: ['invalid-typed-fact'],
    });
    expect(invalid.unprojectedSpans.values).toMatchObject([
      {spanId: 'invalid', reason: 'invalid-attributes'},
    ]);

    const customAttrs = create(AnySchema, {
      typeUrl: 'type.googleapis.com/example.custom.v1.CustomAttrs',
      value: new Uint8Array(),
    });
    const unsupported = projectRunDetail(
      applied(
        ingestRunDetailSkeleton(
          emptyRunDetail(RUN),
          snapshot([
            header(),
            spanRecord({sequence: 2n, id: 'custom', kind: 'custom', attrs: customAttrs}),
          ]),
        ),
      ),
    );
    expect(unsupported.unprojectedSpans.values).toMatchObject([
      {
        spanId: 'custom',
        attrsType: 'example.custom.v1.CustomAttrs',
        reason: 'projector-not-installed',
        source: {sequence: 2n, epoch: 1},
      },
    ]);
  });

  it('bounds each typed span family to its newest replacements', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 5}), fc.integer({min: 1, max: 12}), (bound, extra) => {
        const count = bound + extra;
        const records: StoreRecord[] = [header()];
        for (let index = 0; index < count; index += 1) {
          records.push(
            spanRecord({
              sequence: BigInt(records.length + 1),
              id: `work-${index}`,
              kind: 'workstream',
              attrs: workstreamAttrs({sequence: index + 1}),
            }),
            spanRecord({
              sequence: BigInt(records.length + 2),
              id: `gate-${index}`,
              kind: 'gate',
              attrs: gateAttrs(),
            }),
          );
        }
        const view = projectRunDetail(
          applied(
            ingestRunDetailSkeleton(
              emptyRunDetail(RUN, {
                maxWorkstreams: bound,
                maxCorrectnessGates: bound,
              }),
              snapshot(records),
            ),
          ),
        );
        expect(view.workstreams.values).toHaveLength(bound);
        expect(view.correctness.values).toHaveLength(bound);
        expect(view.workstreams.values.at(-1)?.spanId).toBe(`work-${count - 1}`);
        expect(view.correctness.values.at(-1)?.spanId).toBe(`gate-${count - 1}`);
        expect(view.workstreams.availability).toEqual({
          kind: 'partial',
          reasons: ['state-truncated'],
        });
        expect(view.correctness.availability).toEqual({
          kind: 'partial',
          reasons: ['state-truncated'],
        });
      }),
      {numRuns: 60},
    );
  });

  it('all snapshot and tail splits converge across typed workstream replacements', () => {
    fc.assert(
      fc.property(
        fc.array(fc.string({minLength: 1, maxLength: 24}), {minLength: 1, maxLength: 20}),
        fc.nat(),
        (ideas, rawCut) => {
          const records: StoreRecord[] = [
            header(),
            spanRecord({
              sequence: 2n,
              id: 'changing-work',
              kind: 'workstream',
              attrs: workstreamAttrs({idea: 'initial'}),
            }),
            ...ideas.map((idea, index) =>
              spanRecord({
                sequence: BigInt(index + 3),
                id: 'changing-work',
                kind: 'workstream',
                change: Change.UPDATED,
                attrs: workstreamAttrs({idea, attempts: index + 1}),
              }),
            ),
          ];
          const whole = projectRunDetail(
            applied(ingestRunDetailSkeleton(emptyRunDetail(RUN), snapshot(records))),
          );
          const cut = 2 + (rawCut % (records.length - 1));
          let split = applied(
            ingestRunDetailSkeleton(
              emptyRunDetail(RUN),
              snapshot(records.slice(0, cut), BigInt(records.length)),
            ),
          );
          for (const record of records.slice(cut)) {
            split = ingestRunDetailTail(split, tail([record])).state;
          }
          expect(projectRunDetail(split)).toEqual(whole);
          expect(whole.workstreams.values[0]?.source.sequence).toBe(BigInt(records.length));
        },
      ),
      {numRuns: 80},
    );
  });
});
