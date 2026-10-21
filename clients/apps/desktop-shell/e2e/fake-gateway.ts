import {create, toBinary} from '@bufbuild/protobuf';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  RecordSchema,
  RunLiveness,
  SkeletonResponseSchema,
  TailResponseSchema,
  TailStreamResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import type {StoreRunFixture} from '@vibesys/run-fixtures';

export interface WireFixture {
  methods: Record<'skeleton' | 'tail' | 'blobs' | 'log' | 'hostCall' | 'watchRuns', string>;
  skeleton: number[];
  tail: number[];
  blobs: Record<string, number[]>;
  log: number[];
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
          liveness:
            liveness ??
            (lifecycle?.state.case === 'ended' || lifecycle?.state.case === 'parked'
              ? RunLiveness.ENDED
              : RunLiveness.ALIVE),
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

export function installGateway(input: WireFixture): void {
  const calls: string[] = [];
  const streams = new Set<(bytes: Uint8Array) => void>();
  const watchers = new Set<(bytes: Uint8Array) => void>();
  let active = 0;
  Object.assign(window, {
    vibesysGateway: {
      unary: (host: string, method: string, request: Uint8Array) => {
        calls.push(method);
        if (method === input.methods.hostCall) {
          const peer = window as unknown as {
            postIntent(host: string, bytes: number[]): Promise<number[]>;
          };
          return {
            result: peer.postIntent(host, [...request]).then(bytes => new Uint8Array(bytes)),
            cancel: () => undefined,
          };
        }
        const bytes =
          method === input.methods.skeleton
            ? input.skeleton
            : method === input.methods.blobs
              ? input.blobs[[...request].join(',')]
              : undefined;
        if (!bytes) throw new Error('Unknown unary');
        return {result: Promise.resolve(new Uint8Array(bytes)), cancel: () => undefined};
      },
      stream: (
        _host: string,
        method: string,
        _bytes: Uint8Array,
        receive: (bytes: Uint8Array) => void,
      ) => {
        calls.push(method);
        active += 1;
        let closed = false;
        if (method === input.methods.tail) {
          streams.add(receive);
          receive(new Uint8Array(input.tail));
        } else if (method === input.methods.watchRuns) {
          watchers.add(receive);
          receive(new Uint8Array(input.watchRuns));
        } else if (method === input.methods.log) receive(new Uint8Array(input.log));
        else throw new Error('Unknown stream');
        return {
          cancel: () => {
            if (!closed) {
              closed = true;
              active -= 1;
              streams.delete(receive);
              watchers.delete(receive);
            }
          },
        };
      },
    },
    fakeGateway: {
      calls: () => calls,
      active: () => active,
      observe: (bytes: number[]) => {
        for (const receive of watchers) receive(new Uint8Array(bytes));
      },
      append: (bytes: number[]) => {
        for (const receive of streams) receive(new Uint8Array(bytes));
      },
    },
  });
}
