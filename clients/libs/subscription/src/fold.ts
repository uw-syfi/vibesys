/**
 * The data fold of a subscription: stream updates in, the topic's keyed map out. Entries and
 * removals up to `synced` are one complete snapshot, built apart from the last one and swapped in
 * at `synced`, so a key removed while the stream was away cannot stay behind. Later updates change
 * the swapped-in map. The same reducer runs in main and in every window.
 *
 * A `Fold` is consumed by the step that takes it: the snapshot under construction is extended in
 * place, because copying it per entry would make a large initial snapshot quadratic. A published
 * map is never changed; each later update copies it.
 */
import type {TopicEntry} from '@vibesys/proto';
import type {Control} from './state.js';
import type {AnyTopic} from './topics.js';

/** What a window folds. `control` is not part of the data, so the fold ignores it. */
export type SubscriptionUpdate =
  | {readonly kind: 'control'; readonly control: Control}
  | {readonly kind: 'begin'}
  | {readonly kind: 'entry'; readonly entry: TopicEntry}
  | {readonly kind: 'removed'; readonly key: string}
  | {readonly kind: 'synced'};

/** The updates that change the data. */
export type DataUpdate = Exclude<SubscriptionUpdate, {readonly kind: 'control'}>;

export interface Fold {
  /** The last complete snapshot with later changes applied; null until a stream has synced. */
  readonly map: ReadonlyMap<string, TopicEntry> | null;
  /** The open stream's snapshot until its `synced`; null once synced or while no stream is open. */
  readonly building: Map<string, TopicEntry> | null;
  /** Whether a stream is open. Changes outside one break the protocol. */
  readonly open: boolean;
  /** A frame broke the protocol or a bound. Sticky: nothing after it changes the fold. */
  readonly broken: boolean;
}

export const EMPTY_FOLD: Fold = {map: null, building: null, open: false, broken: false};

const BROKEN = (fold: Fold): Fold => ({...fold, broken: true});

/** Apply one update. Never throws; a protocol violation or a breached bound sets `broken`. */
export function foldUpdate(topic: AnyTopic, fold: Fold, update: DataUpdate): Fold {
  // A new stream starts clean, and does not trust a snapshot that a broken one left behind.
  if (update.kind === 'begin')
    return {map: fold.broken ? null : fold.map, building: new Map(), open: true, broken: false};
  if (fold.broken) return fold;
  switch (update.kind) {
    case 'entry':
      return put(topic, fold, update.entry);
    case 'removed':
      return remove(fold, update.key);
    case 'synced':
      return synced(topic, fold);
  }
}

function put(topic: AnyTopic, fold: Fold, entry: TopicEntry): Fold {
  if (!fold.open || !topic.accepts(entry)) return BROKEN(fold);
  const held = fold.building ?? fold.map;
  // A breach is refused before the entry is kept, so a refused entry changes nothing.
  if (!(held?.has(entry.key) ?? false) && (held?.size ?? 0) >= topic.policy.maxKeys)
    return BROKEN(fold);
  if (fold.building !== null) {
    fold.building.set(entry.key, entry);
    return fold;
  }
  const next = new Map(fold.map);
  next.set(entry.key, entry);
  return {...fold, map: next};
}

function remove(fold: Fold, key: string): Fold {
  if (!fold.open) return BROKEN(fold);
  if (fold.building !== null) {
    fold.building.delete(key);
    return fold;
  }
  const next = new Map(fold.map);
  next.delete(key);
  return {...fold, map: next};
}

function synced(topic: AnyTopic, fold: Fold): Fold {
  if (!fold.open) return BROKEN(fold);
  // A second `synced` claims what is already true.
  if (fold.building === null) return fold;
  const map: ReadonlyMap<string, TopicEntry> = fold.building;
  return topic.fold(map) === null ? BROKEN(fold) : {map, building: null, open: true, broken: false};
}

/** The topic's value of the last complete snapshot, or null before one exists. */
export function snapshotValue<S>(
  topic: {fold(entries: ReadonlyMap<string, TopicEntry>): S | null},
  fold: Fold,
): S | null {
  return fold.map === null ? null : topic.fold(fold.map);
}

/**
 * The updates that rebuild `fold` for a window that joins late: the complete snapshot as one
 * stream, then the one under construction as another, so the window's next live update lands on
 * the stream it belongs to. `only` limits the keys sent, as the window asked.
 */
export function replayUpdates(fold: Fold, only?: string): readonly DataUpdate[] {
  if (fold.broken) return [];
  const updates: DataUpdate[] = [];
  const entries = (map: ReadonlyMap<string, TopicEntry>): DataUpdate[] =>
    [...map.values()]
      .filter(entry => only === undefined || entry.key === only)
      .map(entry => ({kind: 'entry', entry}));
  if (fold.map !== null) updates.push({kind: 'begin'}, ...entries(fold.map), {kind: 'synced'});
  if (fold.building !== null) updates.push({kind: 'begin'}, ...entries(fold.building));
  return updates;
}
