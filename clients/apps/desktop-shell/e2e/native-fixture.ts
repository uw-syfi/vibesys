/** Native fixture data comes from the shipping pure projection, once per derived replay set. */
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  ClientReplaySchema,
  RecordSchema,
  RunEntrySchema,
  RunLiveness,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import {parseFixture, type RunFixture, type StoreRunFixture} from '@vibesys/run-fixtures';
import {
  type DerivedReplaySet,
  derivedReplay,
  derivedVariantsDirectory,
} from '@vibesys/run-fixtures/derived';
import {gatewayScript, wire} from './fake-gateway.js';

export interface NativeFixtureOptions {
  readonly refuseActions?: readonly string[];
  readonly historyPrefix?: number;
}

const root = fileURLToPath(new URL('../../../../', import.meta.url));
const projector = join(root, 'tests/e2e/retained_client_fixture.py');

function hash(bytes: string | Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex');
}

/**
 * The replay of a fixture that differs from its declaration, projected once and kept inside the
 * derived set, so it is exactly as current as the canonical replays beside it.
 */
function variantReplay(fixture: RunFixture) {
  const variants = derivedVariantsDirectory();
  const key = hash(JSON.stringify(fixture));
  if (!existsSync(join(variants, key, '.complete'))) {
    try {
      execFileSync('uv', ['run', '--locked', '--project', root, 'python', projector, variants], {
        input: JSON.stringify([{key, fixture}]),
        maxBuffer: 16 << 20,
        timeout: 120000,
      });
    } catch (error) {
      throw new Error(
        `cannot project the replay of fixture ${fixture.name} (it differs from its declaration): ` +
          `${error instanceof Error ? error.message : error}. The Linux image has no uv; run the ` +
          'spec once on the host so the replay is kept in the derived replay set.',
        {cause: error},
      );
    }
  }
  return fromBinary(ClientReplaySchema, readFileSync(join(variants, key, 'replay.pb')));
}

/**
 * A fixture equal to a declaration reads the replay generated from it, and a set that lacks that
 * replay fails naming it; any other fixture is projected by the same real projector.
 */
function replayFor(fixture: RunFixture) {
  const sources: readonly {data: string; set: DerivedReplaySet}[] = [
    {data: join(root, 'clients/libs/run-fixtures/src/data'), set: 'shared'},
    {data: join(root, 'clients/apps/desktop-shell/e2e/scenarios'), set: 'journeys'},
  ];
  for (const source of sources) {
    const declaration = join(source.data, `${fixture.name}.json`);
    if (!existsSync(declaration)) continue;
    const {
      lineage: _lineage,
      nativeLifecycles: _lifecycles,
      nativeStreams: _streams,
      nativeTransitions: _transitions,
      ...value
    } = JSON.parse(readFileSync(declaration, 'utf8'));
    // A JSON file without events is data for something else, not a declaration with a replay.
    if (!('events' in value)) continue;
    if (JSON.stringify(fixture) === JSON.stringify(parseFixture(value))) {
      return fromBinary(ClientReplaySchema, derivedReplay(source.set, fixture.name));
    }
  }
  return variantReplay(fixture);
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
          observation: replayObservation(replay, source),
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
): number[] {
  const head = replay.batches.find(batch => Number(batch.sourceSequence) === source)?.head;
  return [
    ...toBinary(
      RunEntrySchema,
      create(RunEntrySchema, {
        head,
        liveness: head?.lifecycle?.state.case === 'ended' ? RunLiveness.ENDED : RunLiveness.ALIVE,
      }),
    ),
  ];
}
