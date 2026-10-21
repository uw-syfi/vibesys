import {create, toBinary} from '@bufbuild/protobuf';
import {
  GetBlobsRequestSchema,
  GetBlobsResponseSchema,
  HostService,
  RecordSchema,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
  TailStreamResponseSchema,
} from '@vibesys/proto';
import type {StoreRunFixture} from '@vibesys/run-fixtures';

export interface WireFixture {
  methods: Record<'skeleton' | 'tail' | 'blobs' | 'log' | 'hostCall', string>;
  skeleton: number[];
  tail: number[];
  blobs: Record<string, number[]>;
  log: number[];
  chatRequest: number[];
  chat: number[];
}
export function wire(fixture: StoreRunFixture): WireFixture {
  const encoded = fixture.records.map(record => toBinary(RecordSchema, record));
  const prefix = fixture.records.slice(0, fixture.initialCount);
  const method = (name: keyof typeof HostService.method): string =>
    `/${HostService.typeName}/${HostService.method[name].name}`;
  return {
    methods: {
      skeleton: method('skeleton'),
      tail: method('tail'),
      blobs: method('getBlobs'),
      log: method('tailStream'),
      hostCall: method('hostCall'),
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

export function installGateway(input: WireFixture): void {
  const calls: string[] = [];
  const streams = new Set<(bytes: Uint8Array) => void>();
  const chats = new Set<(bytes: Uint8Array) => void>();
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
          if (
            [..._bytes].slice(-input.chatRequest.length).join(',') === input.chatRequest.join(',')
          ) {
            chats.add(receive);
            receive(new Uint8Array(input.chat));
          } else {
            streams.add(receive);
            receive(new Uint8Array(input.tail));
          }
        } else if (method === input.methods.log) receive(new Uint8Array(input.log));
        else throw new Error('Unknown stream');
        return {
          cancel: () => {
            if (!closed) {
              closed = true;
              active -= 1;
              streams.delete(receive);
              chats.delete(receive);
            }
          },
        };
      },
    },
    fakeGateway: {
      appendChat: (bytes: number[]) => {
        for (const receive of chats) receive(new Uint8Array(bytes));
      },
      calls: () => calls,
      active: () => active,
      append: (bytes: number[]) => {
        for (const receive of streams) receive(new Uint8Array(bytes));
      },
    },
  });
}
