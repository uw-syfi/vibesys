/** Declarative scenarios projected by Python; all journeys use the one gateway. */
import {readFileSync} from 'node:fs';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {Page} from '@playwright/test';
import {ClientReplaySchema, RecordSchema, TailResponseSchema} from '@vibesys/proto';
import {parseFixture, type RunFixture} from '@vibesys/run-fixtures';
import {nativeReplayScript} from './native-fixture.js';

export type Journey =
  | 'trace'
  | 'agents'
  | 'prototype'
  | 'lineage10'
  | 'lineage600'
  | 'live'
  | 'empty'
  | 'quota'
  | 'parked'
  | 'stopped'
  | 'stopped-empty'
  | 'startup';

export function journeyFixture(name: Journey): RunFixture {
  const {
    lineage: _lineage,
    nativeLifecycles: _native,
    nativeStreams: _streams,
    nativeTransitions: _transitions,
    ...fixture
  } = JSON.parse(readFileSync(new URL(`./scenarios/${name}.json`, import.meta.url), 'utf8'));
  return parseFixture(fixture);
}

/** A committed native replay, never TypeScript-authored spans or a second gateway Fake. */
export function journeyScript(name: Journey): string {
  const replay = fromBinary(
    ClientReplaySchema,
    readFileSync(
      new URL(`./scenarios/${name === 'live' ? 'live.initial' : name}.pb`, import.meta.url),
    ),
  );
  return nativeReplayScript(journeyFixture(name), replay);
}

/** Deliver the next committed native records and stream writes, in the original source order. */
export async function appendJourney(page: Page, after: number, through: number): Promise<void> {
  for (let sequence = after + 1; sequence <= through; sequence += 1) {
    const replay = fromBinary(
      ClientReplaySchema,
      readFileSync(new URL(`./scenarios/live.${sequence}.pb`, import.meta.url)),
    );
    const records = replay.batches.flatMap(batch => batch.records);
    const tail = [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          records,
          throughSeq: records.length
            ? fromBinary(RecordSchema, records[records.length - 1] ?? new Uint8Array()).seq
            : 0n,
        }),
      ),
    ];
    const streams = Object.fromEntries(
      Object.entries(replay.streams).map(([id, bytes]) => [id, [...bytes]]),
    );
    await page.evaluate(
      ({tail, streams, hasRecords}) => {
        const peer = (
          window as unknown as {
            fakeGateway: {
              append(bytes: number[]): void;
              appendStreams(streams: Record<string, number[]>): void;
            };
          }
        ).fakeGateway;
        if (hasRecords) peer.append(tail);
        peer.appendStreams(streams);
      },
      {tail, streams, hasRecords: records.length > 0},
    );
  }
}

/** The sequence of the last record a journey's committed replay holds. */
export function journeyHead(name: Journey): bigint {
  const replay = fromBinary(
    ClientReplaySchema,
    readFileSync(new URL(`./scenarios/${name}.pb`, import.meta.url)),
  );
  return replay.batches
    .flatMap(batch => batch.records)
    .reduce((head, bytes) => {
      const {seq} = fromBinary(RecordSchema, bytes);
      return seq > head ? seq : head;
    }, 0n);
}

/** The real skeleton builder appends the requested lifecycle after this scenario's prefix. */
export type NativeJourney = Journey | 'startup-running' | 'startup-draining' | 'startup-ended';
export async function transitionJourney(
  page: Page,
  from: NativeJourney,
  to: NativeJourney,
): Promise<void> {
  const read = (name: NativeJourney) =>
    fromBinary(
      ClientReplaySchema,
      readFileSync(new URL(`./scenarios/${name}.pb`, import.meta.url)),
    );
  const prefix = read(from)
    .batches.flatMap(batch => batch.records)
    .map(bytes => fromBinary(RecordSchema, bytes));
  const after = prefix.at(-1)?.seq ?? 0n;
  const records = read(to)
    .batches.flatMap(batch => batch.records)
    .filter(bytes => fromBinary(RecordSchema, bytes).seq > after);
  const throughSeq = records.length
    ? fromBinary(RecordSchema, records[records.length - 1] ?? new Uint8Array()).seq
    : after;
  const bytes = [
    ...toBinary(TailResponseSchema, create(TailResponseSchema, {records, throughSeq})),
  ];
  await page.evaluate(
    bytes =>
      (window as unknown as {fakeGateway: {append(bytes: number[]): void}}).fakeGateway.append(
        bytes,
      ),
    bytes,
  );
}
