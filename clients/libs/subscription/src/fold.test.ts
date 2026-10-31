import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {MachineStatsSchema, type TopicEntry, TopicEntrySchema} from '@vibesys/proto';
import fc from 'fast-check';
import {
  type DataUpdate,
  EMPTY_FOLD,
  type Fold,
  foldUpdate,
  replayUpdates,
  snapshotValue,
} from './fold.js';
import {machineStats, type Topic} from './topics.js';

/** A topic of several keys, so removal and snapshot swaps have something to show. */
const MANY: Topic<ReadonlyMap<string, TopicEntry>> = {
  ...machineStats,
  policy: {...machineStats.policy, maxKeys: 6},
  fold: entries => entries,
};

const KEYS = ['a', 'b', 'c', 'd', 'e'] as const;

function entry(key: string, sampled: number): TopicEntry {
  return create(TopicEntrySchema, {
    key,
    value: {
      case: 'machineStats',
      value: create(MachineStatsSchema, {sampledAt: timestampFromMs(sampled)}),
    },
  });
}

const CHANGE: fc.Arbitrary<{readonly put: boolean; readonly key: string; readonly at: number}> =
  fc.record({put: fc.boolean(), key: fc.constantFrom(...KEYS), at: fc.integer({min: 0, max: 99})});
const STREAM = fc.record({
  building: fc.array(CHANGE, {maxLength: 8}),
  synced: fc.boolean(),
  after: fc.array(CHANGE, {maxLength: 8}),
});

function updateOf(change: {put: boolean; key: string; at: number}): DataUpdate {
  return change.put
    ? {kind: 'entry', entry: entry(change.key, change.at)}
    : {kind: 'removed', key: change.key};
}

function sameEntries(
  map: ReadonlyMap<string, TopicEntry> | null,
  model: ReadonlyMap<string, TopicEntry> | null,
): void {
  if (map === null || model === null) {
    expect(map).toBe(model);
    return;
  }
  expect([...map.keys()].sort()).toEqual([...model.keys()].sort());
  for (const [key, each] of model) expect(map.get(key)).toBe(each);
}

/** What a producer holds: the last complete snapshot with later changes, and the one being built. */
class Producer {
  published: Map<string, TopicEntry> | null = null;
  building: Map<string, TopicEntry> | null = null;

  step(update: DataUpdate): void {
    if (update.kind === 'begin') this.building = new Map();
    else if (update.kind === 'synced') this.#swap();
    else this.#change(this.building ?? this.published, update);
  }

  #swap(): void {
    if (this.building === null) return;
    this.published = this.building;
    this.building = null;
  }

  #change(into: Map<string, TopicEntry> | null, update: DataUpdate): void {
    if (into === null) return;
    if (update.kind === 'entry') into.set(update.entry.key, update.entry);
    else if (update.kind === 'removed') into.delete(update.key);
  }
}

/** Fold `updates`, checking after each that the fold is what the producer holds. */
function foldAgainst(fold: Fold, producer: Producer, updates: readonly DataUpdate[]): Fold {
  let next = fold;
  for (const update of updates) {
    const before = next.map;
    next = foldUpdate(MANY, next, update);
    producer.step(update);
    sameEntries(next.map, producer.published);
    sameEntries(next.building, producer.building);
    expect(next.broken).toBe(false);
    // The last complete snapshot is untouched until the stream that replaces it is synced.
    if (update.kind !== 'begin' && update.kind !== 'synced' && producer.building !== null)
      expect(next.map).toBe(before);
  }
  return next;
}

function runStreams(streams: readonly (typeof STREAM extends fc.Arbitrary<infer T> ? T : never)[]) {
  const producer = new Producer();
  let fold: Fold = EMPTY_FOLD;
  for (const stream of streams) {
    const updates: DataUpdate[] = [{kind: 'begin'}, ...stream.building.map(updateOf)];
    if (stream.synced) updates.push({kind: 'synced'}, ...stream.after.map(updateOf));
    fold = foldAgainst(fold, producer, updates);
  }
  return {fold, producer};
}

test('property: a snapshot then changes folds to the producer map, and a stream that dies half-way leaves the last snapshot untouched', () => {
  fc.assert(
    fc.property(fc.array(STREAM, {maxLength: 6}), streams => {
      const {fold, producer} = runStreams(streams);
      sameEntries(fold.map, producer.published);
    }),
  );
});

test('a key removed while the stream was away does not stay behind at the next snapshot', () => {
  let fold = foldUpdate(MANY, EMPTY_FOLD, {kind: 'begin'});
  for (const key of ['a', 'b'])
    fold = foldUpdate(MANY, fold, {kind: 'entry', entry: entry(key, 1)});
  fold = foldUpdate(MANY, fold, {kind: 'synced'});
  fold = foldUpdate(MANY, fold, {kind: 'begin'});
  fold = foldUpdate(MANY, fold, {kind: 'entry', entry: entry('a', 2)});
  expect([...(fold.map ?? new Map()).keys()]).toEqual(['a', 'b']);
  fold = foldUpdate(MANY, fold, {kind: 'synced'});
  expect([...(fold.map ?? new Map()).keys()]).toEqual(['a']);
});

const ANY_UPDATE: fc.Arbitrary<DataUpdate> = fc.oneof(
  fc.constant<DataUpdate>({kind: 'begin'}),
  fc.constant<DataUpdate>({kind: 'synced'}),
  fc.constantFrom(...KEYS, 'f', 'g').map((key): DataUpdate => ({kind: 'removed', key})),
  fc
    .tuple(fc.constantFrom(...KEYS, 'f', 'g'), fc.integer({min: 0, max: 9}))
    .map(([key, at]): DataUpdate => ({kind: 'entry', entry: entry(key, at)})),
  fc
    .constantFrom(...KEYS)
    .map((key): DataUpdate => ({kind: 'entry', entry: create(TopicEntrySchema, {key})})),
);

/** What one step of an arbitrary sequence must keep true. */
function checkStep(before: Fold, update: DataUpdate, after: Fold, first: boolean): void {
  if (before.broken && update.kind !== 'begin') expect(after).toBe(before);
  if (before.broken && update.kind === 'begin') expect(after.map).toBeNull();
  // No stream is open before the first `begin`, so nothing else may arrive.
  if (first && update.kind !== 'begin') expect(after.broken).toBe(true);
  // A published snapshot is never edited, only replaced.
  if (before.map !== null && after.map === before.map) expect(after.map.size).toBe(before.map.size);
  expect(after.map?.size ?? 0).toBeLessThanOrEqual(MANY.policy.maxKeys);
  expect(after.building?.size ?? 0).toBeLessThanOrEqual(MANY.policy.maxKeys);
}

test('property: any update sequence folds without throwing, and a broken fold stays as it is', () => {
  fc.assert(
    fc.property(fc.array(ANY_UPDATE, {maxLength: 30}), updates => {
      let fold: Fold = EMPTY_FOLD;
      for (const [index, update] of updates.entries()) {
        const next = foldUpdate(MANY, fold, update);
        checkStep(fold, update, next, index === 0);
        fold = next;
      }
    }),
  );
});

function keyed(
  map: ReadonlyMap<string, TopicEntry> | null,
  only: string | undefined,
): ReadonlyMap<string, TopicEntry> | null {
  if (map === null) return null;
  return new Map([...map].filter(([key]) => only === undefined || key === only));
}

test('property: replaying a fold into an empty one rebuilds the same snapshots, whole or for one key', () => {
  fc.assert(
    fc.property(
      fc.array(STREAM, {maxLength: 4}),
      fc.option(fc.constantFrom(...KEYS), {nil: undefined}),
      (streams, only) => {
        const {fold} = runStreams(streams);
        let joined: Fold = EMPTY_FOLD;
        for (const update of replayUpdates(fold, only)) joined = foldUpdate(MANY, joined, update);
        sameEntries(joined.map, keyed(fold.map, only));
        sameEntries(joined.building, keyed(fold.building, only));
        expect(joined.broken).toBe(false);
      },
    ),
  );
});

/** A fold is consumed by the step that takes it, so each case starts its own. */
function begun(topic: Topic<unknown>): Fold {
  return foldUpdate(topic, EMPTY_FOLD, {kind: 'begin'});
}

test('a topic takes only its own entries and its own number of keys', () => {
  const empty = create(TopicEntrySchema, {key: 'm'});
  expect(foldUpdate(machineStats, begun(machineStats), {kind: 'entry', entry: empty}).broken).toBe(
    true,
  );
  const one = foldUpdate(machineStats, begun(machineStats), {kind: 'entry', entry: entry('m', 1)});
  expect(one.broken).toBe(false);
  const refused = foldUpdate(machineStats, one, {kind: 'entry', entry: entry('n', 1)});
  expect(refused.broken).toBe(true);
  // The refused entry was not kept.
  expect([...(one.building ?? new Map()).keys()]).toEqual(['m']);
});

test('a snapshot is a value of its topic only if the topic folds it', () => {
  expect(foldUpdate(machineStats, begun(machineStats), {kind: 'synced'}).broken).toBe(true);
  const one = foldUpdate(machineStats, begun(machineStats), {kind: 'entry', entry: entry('m', 1)});
  const synced = foldUpdate(machineStats, one, {kind: 'synced'});
  expect(snapshotValue(machineStats, synced)?.sampledAt?.nanos).toBe(1_000_000);
  expect(foldUpdate(machineStats, EMPTY_FOLD, {kind: 'synced'}).broken).toBe(true);
  expect(snapshotValue(machineStats, EMPTY_FOLD)).toBeNull();
});
