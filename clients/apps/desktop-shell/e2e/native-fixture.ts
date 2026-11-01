/** Native fixture data comes from the shipping pure projection, once per derived replay set. */
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  ClientReplaySchema,
  RecordSchema,
  RunEntrySchema,
  RunLiveness,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import type {RunFixture, StoreRunFixture} from '@vibesys/run-fixtures';
import {derivedVariantsDirectory} from '@vibesys/run-fixtures/derived';
import {declaredReplay, ROOT, recordsOf} from '../src/testing/native-records.js';
import {gatewayScript, wire} from './fake-gateway.js';

const projector = join(ROOT, 'tests/e2e/retained_client_fixture.py');

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
      execFileSync('uv', ['run', '--locked', '--project', ROOT, 'python', projector, variants], {
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

/** The replay of `fixture`: the one generated from its declaration, else the one projected for it. */
function replayFor(fixture: RunFixture) {
  return declaredReplay(fixture) ?? variantReplay(fixture);
}

/** Every record the shipping projection makes of the fixture's run, in log order. */
export function nativeRecords(fixture: RunFixture): StoreRecord[] {
  return recordsOf(replayFor(fixture));
}

export interface NativeFixtureOptions {
  readonly refuseActions?: readonly string[];
  readonly historyPrefix?: number;
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
