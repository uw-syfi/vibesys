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
  behavior?: GatewayBehavior;
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

/** How the gateway misbehaves: a refusal carries its gRPC status; the Tail can be held back. */
export interface GatewayBehavior {
  readonly skeleton?: {readonly status: number; readonly message: string};
  readonly holdTail?: boolean;
}

export function installGateway(input: WireFixture): void {
  const calls: string[] = [];
  const streams = new Set<(bytes: Uint8Array) => void>();
  const ended = new Set<(error?: string) => void>();
  const history = [input.tail];
  const chats = new Set<(bytes: Uint8Array) => void>();
  const watchers = new Set<(bytes: Uint8Array) => void>();
  const held = new Set<(bytes: Uint8Array) => void>();
  const openTail = (request: Uint8Array, receive: (bytes: Uint8Array) => void): void => {
    if ([...request].slice(-input.chatRequest.length).join(',') === input.chatRequest.join(',')) {
      chats.add(receive);
      receive(new Uint8Array(input.chat));
      return;
    }
    streams.add(receive);
    if (input.behavior?.holdTail) held.add(receive);
    else for (const bytes of history) receive(new Uint8Array(bytes));
  };
  let active = 0;
  const answer = (reply: unknown) => ({
    result: Promise.resolve(reply),
    cancel: (): void => undefined,
  });
  Object.assign(window, {
    vibesysGateway: {
      unary: (host: string, method: string, request: Uint8Array) => {
        calls.push(method);
        const refused = input.behavior?.skeleton;
        if (method === input.methods.skeleton && refused)
          return answer({error: refused.message, status: refused.status});
        if (method === input.methods.hostCall) {
          const peer = window as unknown as {
            postIntent(host: string, bytes: number[]): Promise<number[]>;
          };
          return answer(
            peer.postIntent(host, [...request]).then(bytes => ({bytes: new Uint8Array(bytes)})),
          );
        }
        const bytes =
          method === input.methods.skeleton
            ? input.skeleton
            : method === input.methods.blobs
              ? input.blobs[[...request].join(',')]
              : undefined;
        if (!bytes) throw new Error('Unknown unary');
        return answer({bytes: new Uint8Array(bytes)});
      },
      stream: (
        _host: string,
        method: string,
        _bytes: Uint8Array,
        receive: (bytes: Uint8Array) => void,
        finish: (error?: string) => void,
      ) => {
        ended.add(finish);
        calls.push(method);
        active += 1;
        if (method === input.methods.tail) {
          openTail(_bytes, receive);
        } else if (method === input.methods.watchRuns) {
          watchers.add(receive);
          receive(new Uint8Array(input.watchRuns));
        } else if (method === input.methods.log) receive(new Uint8Array(input.log));
        else throw new Error('Unknown stream');
        return {
          cancel: () => {
            if (!ended.delete(finish)) return;
            active -= 1;
            streams.delete(receive);
            held.delete(receive);
            chats.delete(receive);
            watchers.delete(receive);
          },
        };
      },
    },
    fakeGateway: {
      releaseTail: () => {
        for (const receive of held) for (const bytes of history) receive(new Uint8Array(bytes));
        held.clear();
      },
      appendChat: (bytes: number[]) => {
        for (const receive of chats) receive(new Uint8Array(bytes));
      },
      disconnect: () => {
        for (const finish of [...ended]) finish('SSH connection closed');
      },
      calls: () => calls,
      active: () => active,
      observe: (bytes: number[]) => {
        for (const receive of watchers) receive(new Uint8Array(bytes));
      },
      append: (bytes: number[]) => {
        history.push(bytes);
        for (const receive of streams) receive(new Uint8Array(bytes));
      },
    },
  });
}
