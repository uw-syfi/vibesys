/** Native fixture data comes from the shipping pure projection, once per content hash. */
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  API_SCHEMA_HASH,
  ClientReplaySchema,
  RecordSchema,
  RunLiveness,
  STORE_SCHEMA_HASH,
  type Record as StoreRecord,
  TailResponseSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import {parseFixture, type RunFixture, type StoreRunFixture} from '@vibesys/run-fixtures';
import {gatewayScript, wire} from './fake-gateway.js';

export interface NativeFixtureOptions {
  readonly refuseActions?: readonly string[];
  readonly historyPrefix?: number;
}

const root = fileURLToPath(new URL('../../../../', import.meta.url));
const projector = join(root, 'tests/e2e/retained_client_fixture.py');
const cache = join(
  tmpdir(),
  `vibesys-native-fixtures-${hash(
    [
      API_SCHEMA_HASH,
      STORE_SCHEMA_HASH,
      readFileSync(projector).toString(),
      readFileSync(join(root, 'tests/support/retained_events.py')).toString(),
      ...[
        ...files(join(root, 'libs/vs-runlog/src/vs_runlog')),
        ...files(join(root, 'src/vibesys/records')),
      ]
        .filter(path => path.endsWith('.py'))
        .sort()
        .map(path => readFileSync(path).toString()),
    ].join('\n'),
  )}`,
);
function hash(bytes: string | Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex');
}
function files(root: string): string[] {
  if (!existsSync(root)) return [];
  return readdirSync(root, {recursive: true, withFileTypes: true})
    .filter(entry => entry.isFile())
    .map(entry => join(entry.parentPath, entry.name));
}
function ensure(fixtures: readonly RunFixture[]): void {
  const missing = fixtures
    .map(fixture => ({key: hash(JSON.stringify(fixture)), fixture}))
    .filter(item => !existsSync(join(cache, item.key, '.complete')));
  if (missing.length === 0) return;
  execFileSync('uv', ['run', '--locked', '--project', root, 'python', projector, cache], {
    input: JSON.stringify(missing),
    maxBuffer: 16 << 20,
    timeout: 120000,
  });
}
/** Canonical scenarios use checked native goldens; custom variants use the same real projector. */
function replayFor(fixture: RunFixture) {
  const sources = [
    {
      data: join(root, 'clients/libs/run-fixtures/src/data'),
      native: join(root, 'clients/libs/run-fixtures/src/native'),
    },
    {
      data: join(root, 'clients/apps/desktop-shell/e2e/scenarios'),
      native: join(root, 'clients/apps/desktop-shell/e2e/scenarios'),
    },
  ];
  for (const source of sources) {
    const declaration = join(source.data, `${fixture.name}.json`);
    const golden = join(source.native, `${fixture.name}.pb`);
    if (!existsSync(declaration) || !existsSync(golden)) continue;
    const {
      lineage: _lineage,
      nativeLifecycles: _lifecycles,
      nativeStreams: _streams,
      nativeTransitions: _transitions,
      ...value
    } = JSON.parse(readFileSync(declaration, 'utf8'));
    if (JSON.stringify(fixture) === JSON.stringify(parseFixture(value))) {
      return fromBinary(ClientReplaySchema, readFileSync(golden));
    }
  }
  ensure([fixture]);
  return fromBinary(
    ClientReplaySchema,
    readFileSync(join(cache, hash(JSON.stringify(fixture)), 'replay.pb')),
  );
}
/** Every record the shipping projection makes of the fixture's run, in log order. */
export function nativeRecords(fixture: RunFixture): StoreRecord[] {
  return replayFor(fixture)
    .batches.flatMap(batch => batch.records.map(bytes => fromBinary(RecordSchema, bytes)))
    .sort((a, b) => Number(a.seq - b.seq));
}
export function nativeFixtureScript(
  fixture: RunFixture,
  options: NativeFixtureOptions = {},
): string {
  return nativeReplayScript(fixture, replayFor(fixture), options);
}

/** Serve Python-produced records through the same gateway for recorded and declarative fixtures. */
export function nativeReplayScript(
  fixture: RunFixture,
  replay: import('@bufbuild/protobuf').MessageShape<typeof ClientReplaySchema>,
  options: NativeFixtureOptions = {},
): string {
  const groups = new Map(
    replay.batches.map(batch => [
      Number(batch.sourceSequence),
      batch.records.map(bytes => fromBinary(RecordSchema, bytes)),
    ]),
  );
  const held = options.historyPrefix !== undefined;
  const through = fixture.state === 'live' ? (fixture.liveAfter ?? Infinity) : Infinity;
  const records = [...groups]
    .filter(([source]) => source <= through)
    .flatMap(([, rows]) => rows)
    .sort((a, b) => Number(a.seq - b.seq));
  const stored: StoreRunFixture = {
    records,
    initialCount: options.historyPrefix ?? records.length,
    blobs: replay.blobs,
    streams: replay.streams,
  };
  const head = replay.batches.filter(batch => Number(batch.sourceSequence) <= through).at(-1)?.head;
  const input = wire(
    stored,
    fixture.state === 'live' ? RunLiveness.ALIVE : RunLiveness.ENDED,
    head,
  );
  input.behavior = {
    ...(held ? {holdTail: true} : {}),
    ...(options.refuseActions ? {refuseActions: options.refuseActions} : {}),
  };
  const updates = Object.fromEntries(
    [...groups]
      .filter(([source, rows]) => source > through && rows.length > 0)
      .map(([source, rows]) => [
        source,
        {
          tail: [
            ...toBinary(
              TailResponseSchema,
              create(TailResponseSchema, {
                records: rows
                  .sort((a, b) => Number(a.seq - b.seq))
                  .map(row => toBinary(RecordSchema, row)),
                throughSeq: rows.at(-1)?.seq ?? 0n,
              }),
            ),
          ],
          observation: replayObservation(replay, source, fixture.runId),
        },
      ]),
  );
  return `if (!window.vibesysGateway) {${gatewayScript(input)}\n(${connectUpdates.toString()})(${JSON.stringify(updates)});}`;
}
function connectUpdates(updates: Record<string, {tail: number[]; observation: number[]}>): void {
  const peer = window as unknown as {
    fakeGateway: {
      append(bytes: number[]): void;
      observe(bytes: number[]): void;
      disconnect(): void;
    };
    fakeHost: {emit(events: readonly {sequence?: number}[]): void; drop(): void};
  };
  peer.fakeHost.emit = events => {
    for (const event of events) {
      const update = updates[String(event.sequence)];
      if (update) {
        peer.fakeGateway.append(update.tail);
        peer.fakeGateway.observe(update.observation);
      }
    }
  };
  peer.fakeHost.drop = () => {
    peer.fakeGateway.disconnect();
  };
}

function replayObservation(
  replay: import('@bufbuild/protobuf').MessageShape<typeof ClientReplaySchema>,
  source: number,
  runId: string,
): number[] {
  const head = replay.batches.find(batch => Number(batch.sourceSequence) === source)?.head;
  return [
    ...toBinary(
      WatchRunsResponseSchema,
      create(WatchRunsResponseSchema, {
        runId,
        head,
        liveness: head?.lifecycle?.state.case === 'ended' ? RunLiveness.ENDED : RunLiveness.ALIVE,
      }),
    ),
  ];
}
