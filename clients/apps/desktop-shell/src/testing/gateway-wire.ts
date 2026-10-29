import {create, toBinary} from '@bufbuild/protobuf';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  RecordSchema,
  RunLiveness,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import type {StoreRunFixture} from '@vibesys/run-fixtures';
import {epochHasEnded} from '@vibesys/run-skeleton';

export interface WireFixture {
  methods: Record<'skeleton' | 'tail' | 'blobs' | 'log' | 'hostCall' | 'watchRuns', string>;
  skeleton: number[];
  tail: number[];
  blobs: Record<string, number[]>;
  log: number[];
  chatRequest: number[];
  chat: number[];
  watchRuns: number[];
}
export function wire(fixture: StoreRunFixture, liveness?: RunLiveness): WireFixture {
  const encoded = fixture.records.map(record => toBinary(RecordSchema, record));
  const prefix = fixture.records.slice(0, fixture.initialCount);
  const method = (name: keyof typeof HostService.method): string =>
    `/${HostService.typeName}/${HostService.method[name].name}`;
  const last = fixture.records.filter(record => record.body.case === 'lifecycle').at(-1);
  const lifecycle = last?.body.case === 'lifecycle' ? last.body.value : undefined;
  const epoch = fixture.records.at(-1)?.epoch ?? 1;
  return {
    watchRuns: [
      ...toBinary(
        WatchRunsResponseSchema,
        create(WatchRunsResponseSchema, {
          runId: 'run-a',
          liveness: liveness ?? (epochHasEnded(lifecycle) ? RunLiveness.ENDED : RunLiveness.ALIVE),
          head: {
            strategy: 'fixture',
            lifecycle,
            process: {pid: 1, epoch, startedAt: {seconds: 1700000000n}},
          },
        }),
      ),
    ],
    methods: {
      skeleton: method('skeleton'),
      tail: method('tail'),
      blobs: method('getBlobs'),
      log: method('tailStream'),
      hostCall: method('hostCall'),
      watchRuns: method('watchRuns'),
    },
    skeleton: [
      ...toBinary(
        SkeletonResponseSchema,
        create(SkeletonResponseSchema, {
          records: encoded.slice(0, fixture.initialCount),
          committedSeq: fixture.records.at(-1)?.seq ?? 0n,
          nextSeq: (prefix.at(-1)?.seq ?? 0n) + 1n,
          complete: fixture.initialCount === fixture.records.length,
        }),
      ),
    ],
    tail: [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          records: encoded.slice(fixture.initialCount),
          throughSeq: fixture.records.at(-1)?.seq ?? 0n,
        }),
      ),
    ],
    blobs: Object.fromEntries(
      Object.entries(fixture.blobs).map(([sha256, data]) => [
        [
          ...toBinary(
            GetBlobsRequestSchema,
            create(GetBlobsRequestSchema, {runId: 'run-a', sha256: [sha256]}),
          ),
        ].join(','),
        [
          ...toBinary(
            GetBlobsResponseSchema,
            create(GetBlobsResponseSchema, {blobs: [{sha256, data}]}),
          ),
        ],
      ]),
    ),
    chatRequest: [
      ...toBinary(TailRequestSchema, create(TailRequestSchema, {sessionId: 'default'})),
    ],
    chat: [],
    log: [
      ...toBinary(
        TailStreamResponseSchema,
        create(TailStreamResponseSchema, {
          data: fixture.streams['build-log'] ?? new Uint8Array(),
          nextOffset: BigInt(fixture.streams['build-log']?.length ?? 0),
        }),
      ),
    ],
  };
}
