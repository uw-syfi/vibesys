import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {equals, fromJson, type JsonValue, toBinary} from '@bufbuild/protobuf';
import {
  MachineStatsSchema,
  type SubscribeResponse,
  SubscribeResponseSchema,
  type TopicEntry,
  validatedFromBinary,
} from '@vibesys/proto';
import {
  backoffMs,
  EMPTY_FOLD,
  type Fold,
  foldUpdate,
  type StreamEvent,
  snapshotValue,
  streamEvent,
} from './index.js';
import {Harness, LEASE_ONLY} from './testing/harness.js';

/** What the real hub sent for scripted producers (`gateway/internal/subscription/corpus_test.go`). */
interface Corpus {
  readonly max_keys: number;
  readonly cases: readonly {
    readonly name: string;
    readonly streams: readonly {
      readonly frames: readonly JsonValue[];
      readonly producer: Readonly<Record<string, JsonValue>>;
      readonly ends: 'open' | 'failed';
    }[];
  }[];
}

const CORPUS: Corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dir, '../../../../tests/conformance/fixtures/subscription/frames.json'),
    'utf8',
  ),
);

/** A frame as a client receives it: bytes the generated schema accepts. */
function received(json: JsonValue): SubscribeResponse {
  return validatedFromBinary(
    SubscribeResponseSchema,
    toBinary(SubscribeResponseSchema, fromJson(SubscribeResponseSchema, json)),
  );
}

function sameAsProducer(
  map: ReadonlyMap<string, TopicEntry> | null,
  producer: Readonly<Record<string, JsonValue>>,
): void {
  expect([...(map ?? new Map()).keys()].sort()).toEqual(Object.keys(producer).sort());
  for (const [key, json] of Object.entries(producer)) {
    const held = map?.get(key)?.value;
    if (held?.case !== 'machineStats') throw new Error(`${key} holds no machine stats`);
    expect(equals(MachineStatsSchema, held.value, fromJson(MachineStatsSchema, json))).toBe(true);
  }
}

test('the corpus holds the cases the hub promises', () => {
  expect(CORPUS.cases.map(each => each.name)).toContain(
    "a new stream starts from the producer's map, not the last stream's",
  );
  expect(CORPUS.cases.length).toBeGreaterThanOrEqual(8);
});

for (const each of CORPUS.cases) {
  test(`the fold of "${each.name}" is the producer's map after every stream`, () => {
    const topic = {...LEASE_ONLY, policy: {...LEASE_ONLY.policy, maxKeys: CORPUS.max_keys}};
    let fold: Fold = EMPTY_FOLD;
    for (const stream of each.streams) {
      fold = foldUpdate(topic, fold, {kind: 'begin'});
      for (const json of stream.frames) {
        const event = streamEvent(received(json), 0);
        if (event.type === 'entry')
          fold = foldUpdate(topic, fold, {kind: 'entry', entry: event.entry});
        if (event.type === 'removed')
          fold = foldUpdate(topic, fold, {kind: 'removed', key: event.key});
        if (event.type === 'synced') fold = foldUpdate(topic, fold, {kind: 'synced'});
        expect(fold.broken).toBe(false);
      }
      sameAsProducer(fold.map, stream.producer);
    }
  });
}

async function settle(): Promise<void> {
  for (let drain = 0; drain < 5; drain += 1) await Promise.resolve();
}

/** What a frame means to the machine, with the time it arrived. */
function eventOf(json: JsonValue, at: number): StreamEvent {
  return streamEvent(received(json), at);
}

for (const each of CORPUS.cases) {
  test(`the machine over "${each.name}" is live with the producer's map, and stale if the topic failed`, async () => {
    const topic = {...LEASE_ONLY, policy: {...LEASE_ONLY.policy, maxKeys: CORPUS.max_keys}};
    const harness = new Harness(topic);
    for (const [index, stream] of each.streams.entries()) {
      await settle();
      const open = harness.latest;
      open.reach('stream');
      for (const json of stream.frames) {
        harness.clock.advanceBy(1_000);
        open.send(eventOf(json, harness.clock.now()));
      }
      const failed = stream.ends === 'failed';
      expect(harness.control.kind).toBe(failed ? 'stale' : 'live');
      const fold = harness.actor.getSnapshot().context.fold;
      sameAsProducer(snapshotValue(topic, fold), stream.producer);
      if (index < each.streams.length - 1) {
        // The client leaves, or the connection drops; the next stream starts from nothing.
        open.fail();
        await settle();
        harness.clock.advanceBy(backoffMs(1));
      }
    }
  });
}
