/**
 * Serializable persistent storage for append-heavy reducer arrays.
 *
 * A 32-way tree gives indexed reads and immutable replacement in a few node
 * copies at supported run sizes. The tree is plain structured-clone and JSON
 * data. Callers explicitly materialize it only at a publication boundary.
 */

const BRANCH_FACTOR = 32;

type PersistentArrayNode<T> = readonly (PersistentArrayNode<T> | T | null | undefined)[];

export interface PersistentArray<T> {
  readonly root: PersistentArrayNode<T>;
  readonly depth: number;
  readonly length: number;
}

// Optional and correctness-neutral: the tree remains the sole source of truth.
// Non-enumerability keeps this cache out of JSON and structured-clone payloads.
const MATERIALIZE = 'materializePersistentArray';

interface MaterializedPersistentArray<T> extends PersistentArray<T> {
  readonly [MATERIALIZE]?: () => T[];
}

/** Returns one entry without materializing the persistent array. */
export function persistentArrayAt<T>(values: PersistentArray<T>, index: number): T | undefined {
  if (!Number.isSafeInteger(index) || index < 0 || index >= values.length) return undefined;
  let node = values.root;
  for (let level = values.depth; level > 0; level -= 1) {
    const child = node[slotAt(index, level)];
    if (!Array.isArray(child)) return undefined;
    node = child as PersistentArrayNode<T>;
  }
  return node[slotAt(index, 0)] as T | undefined;
}

/** Replaces one entry without copying the complete retained history. */
export function replacePersistentArrayEntry<T>(
  values: PersistentArray<T>,
  index: number,
  value: T,
): PersistentArray<T> {
  if (index < 0 || index >= values.length) {
    throw new RangeError(`Invalid persistent-array index ${index}`);
  }
  if (persistentArrayAt(values, index) === value) return values;
  return setPersistentArrayEntry(values, index, value);
}

/** Stores an entry at a direct key; intended for sparse indexes, not materialization. */
export function setPersistentArrayIndex<T>(
  values: PersistentArray<T>,
  index: number,
  value: T,
): PersistentArray<T> {
  if (!Number.isSafeInteger(index) || index < 0) {
    throw new RangeError(`Invalid persistent-array index ${index}`);
  }
  if (persistentArrayAt(values, index) === value) return values;
  return setPersistentArrayEntry(values, index, value);
}

/** Appends one entry while preserving insertion order. */
export function appendPersistentArrayEntry<T>(
  values: PersistentArray<T>,
  value: T,
): PersistentArray<T> {
  return setPersistentArrayEntry(values, values.length, value);
}

/** Builds persistent storage after a whole-collection operation. */
export function persistentArrayFrom<T>(values: readonly T[]): PersistentArray<T> {
  let result = createPersistentArray<T>([], 0, 0);
  for (const value of values) result = setPersistentArrayEntry(result, result.length, value);
  return result;
}

/** Materializes an ordinary array for a consumer-facing projection. */
export function materializePersistentArray<T>(values: PersistentArray<T>): T[] {
  const materialize = (values as MaterializedPersistentArray<T>)[MATERIALIZE];
  return typeof materialize === 'function' ? materialize() : materializeEntries(values);
}

/** Restores the non-serialized materializer after a clone boundary. */
export function rehydratePersistentArray<T>(values: PersistentArray<T>): PersistentArray<T> {
  return typeof (values as MaterializedPersistentArray<T>)[MATERIALIZE] === 'function'
    ? values
    : createPersistentArray(values.root, values.depth, values.length);
}

function materializeEntries<T>(values: PersistentArray<T>): T[] {
  return Array.from({length: values.length}, (_, index) => {
    const value = persistentArrayAt(values, index);
    if (value === undefined) throw new Error(`Persistent array is sparse at index ${index}`);
    return value;
  });
}

function setPersistentArrayEntry<T>(
  values: PersistentArray<T>,
  index: number,
  value: T,
): PersistentArray<T> {
  let root = values.root;
  let depth = values.depth;
  while (index >= capacity(depth)) {
    root = [root];
    depth += 1;
  }
  return createPersistentArray(
    setNode(root, depth, index, value),
    depth,
    Math.max(values.length, index + 1),
  );
}

function createPersistentArray<T>(
  root: PersistentArrayNode<T>,
  depth: number,
  length: number,
): PersistentArray<T> {
  const values: PersistentArray<T> = {root, depth, length};
  let materialized: T[] | undefined;
  Object.defineProperty(values, MATERIALIZE, {
    configurable: true,
    value: () => (materialized ??= materializeEntries(values)),
  });
  return values;
}

function setNode<T>(
  node: PersistentArrayNode<T>,
  level: number,
  index: number,
  value: T,
): PersistentArrayNode<T> {
  const slot = slotAt(index, level);
  // Explicit nulls keep the serializable tree byte-stable across JSON, whose
  // array encoding otherwise turns sparse/undefined positions into null.
  const next: Array<PersistentArrayNode<T> | T | null | undefined> = Array.from(
    {length: Math.max(node.length, slot + 1)},
    (_, position) => node[position] ?? null,
  );
  if (level === 0) {
    next[slot] = value;
    return next;
  }
  const child = node[slot];
  next[slot] = setNode(
    Array.isArray(child) ? (child as PersistentArrayNode<T>) : [],
    level - 1,
    index,
    value,
  );
  return next;
}

function capacity(depth: number): number {
  return BRANCH_FACTOR ** (depth + 1);
}

function slotAt(index: number, level: number): number {
  return Math.floor(index / BRANCH_FACTOR ** level) % BRANCH_FACTOR;
}
