/**
 * What the Fake gateway serves of one run, as bytes: the frames of the generated host RPCs, built
 * from the records the shipping projection makes of a fixture. Nothing of a browser is in it, so
 * the browser journeys (`e2e/fake-gateway.ts` bundles the Fake into the page) and the DOM tests of
 * the shell (`shell-world.tsx` installs the same Fake in happy-dom) serve a run the same way.
 */
import {create, type DescMessage, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  RecordSchema,
  RunEntrySchema,
  type RunHead,
  RunLiveness,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamResponseSchema,
} from '@vibesys/proto';
import type {StoreRunFixture} from '@vibesys/run-fixtures';
import {epochHasEnded} from '@vibesys/run-skeleton';

export interface WireFixture {
  methods: Record<'skeleton' | 'tail' | 'blobs' | 'log' | 'hostCall', string>;
  skeleton: number[];
  tail: number[];
  blobs: Record<string, number[]>;
  log: number[];
  streams?: Record<string, number[]>;
  chatRequest: number[];
  chat: number[];
  /** The run this world is about, which the host's liveness is stated for. */
  runId: string;
  /** What the host says of the run: a `RunEntry`. */
  run: number[];
  behavior?: GatewayBehavior;
}

/** The delivery seam shared with the native gateway world in desktop-main. */
export interface GatewayBehavior {
  readonly holdTail?: boolean;
  readonly skeleton?: {readonly status: number; readonly message: string};
  readonly refuseActions?: readonly string[];
}
const encode = <S extends DescMessage>(schema: S, init: MessageInitShape<S>): number[] => [
  ...toBinary(schema, create(schema, init)),
];

export function wire(
  fixture: StoreRunFixture,
  liveness?: RunLiveness,
  head?: RunHead,
): WireFixture {
  const epochRecord = fixture.records.find(record => record.body.case === 'epochStarted');
  if (epochRecord?.body.case !== 'epochStarted') throw new Error('Fixture has no native epoch');
  const runId = epochRecord.body.value.runId;
  const encoded = fixture.records.map(record => toBinary(RecordSchema, record));
  const prefix = fixture.records.slice(0, fixture.initialCount);
  const method = (name: keyof typeof HostService.method): string =>
    `/${HostService.typeName}/${HostService.method[name].name}`;
  const last = fixture.records.filter(record => record.body.case === 'lifecycle').at(-1);
  const lifecycle = last?.body.case === 'lifecycle' ? last.body.value : undefined;
  const epoch = fixture.records.at(-1)?.epoch ?? 1;
  const throughSeq = fixture.records.at(-1)?.seq ?? 0n;
  const stream = (data: Uint8Array) =>
    encode(TailStreamResponseSchema, {data, nextOffset: BigInt(data.length)});
  return {
    runId,
    run: encode(RunEntrySchema, {
      projectRoot: epochRecord.body.value.projectRoot ?? '/project',
      liveness: liveness ?? (epochHasEnded(lifecycle) ? RunLiveness.ENDED : RunLiveness.ALIVE),
      head: head ?? {
        strategy: 'fixture',
        epoch,
        lifecycle,
        process: {pid: 1, epoch, startedAt: {seconds: 1700000000n}},
      },
    }),
    methods: {
      skeleton: method('skeleton'),
      tail: method('tail'),
      blobs: method('getBlobs'),
      log: method('tailStream'),
      hostCall: method('hostCall'),
    },
    skeleton: encode(SkeletonResponseSchema, {
      records: encoded.slice(0, fixture.initialCount),
      committedSeq: throughSeq,
      nextSeq: (prefix.at(-1)?.seq ?? 0n) + 1n,
      complete: fixture.initialCount === fixture.records.length,
    }),
    tail: encode(TailResponseSchema, {records: encoded.slice(fixture.initialCount), throughSeq}),
    blobs: Object.fromEntries(
      Object.entries(fixture.blobs).map(([sha256, data]) => [
        encode(GetBlobsRequestSchema, {runId, sha256: [sha256]}).join(','),
        encode(GetBlobsResponseSchema, {blobs: [{sha256, data}]}),
      ]),
    ),
    chatRequest: encode(TailRequestSchema, {sessionId: 'default'}),
    chat: [],
    streams: Object.fromEntries(
      Object.entries(fixture.streams).map(([id, data]) => [id, stream(data)]),
    ),
    log: stream(fixture.streams['build-log'] ?? new Uint8Array()),
  };
}
