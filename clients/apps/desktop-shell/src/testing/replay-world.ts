/**
 * What the Fake gateway serves of a run replay, and how it is fed the rest of a live run: the
 * data half of `e2e/native-fixture.ts`, with nothing of a browser or a process in it, so the browser
 * journeys bundle it into the page and the DOM tests of the shell install it in happy-dom.
 */
import {create, fromBinary, type MessageShape, toBinary} from '@bufbuild/protobuf';
import {
  type ClientReplaySchema,
  RecordSchema,
  RunEntrySchema,
  RunLiveness,
  TailResponseSchema,
} from '@vibesys/proto';
import type {RunFixture, StoreRunFixture} from '@vibesys/run-fixtures';
import {type WireFixture, wire} from './gateway-fixture.js';

type Replay = MessageShape<typeof ClientReplaySchema>;

export interface NativeFixtureOptions {
  readonly refuseActions?: readonly string[];
  readonly historyPrefix?: number;
}

/** The records a live run delivers after its first batch, by the source sequence that carries them. */
export type ReplayUpdates = Record<string, {tail: number[]; observation: number[]}>;

export interface ReplayWorld {
  readonly input: WireFixture;
  readonly updates: ReplayUpdates;
}

/** The world the Fake gateway serves for a Python-produced replay of `fixture`. */
export function replayWorld(
  fixture: RunFixture,
  replay: Replay,
  options: NativeFixtureOptions = {},
): ReplayWorld {
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
  return {input, updates};
}

/**
 * Makes `fakeHost.emit` deliver `updates` through the Fake gateway installed on the window. It is
 * self-contained, because the browser journeys serialize it into the page.
 */
export function connectUpdates(updates: ReplayUpdates): void {
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

function replayObservation(replay: Replay, source: number): number[] {
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
