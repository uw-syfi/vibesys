/**
 * Persistent storage for the run map's internal arrays.
 *
 * A 32-way tree gives indexed reads and immutable replacement in at most a few
 * node copies for the run sizes we support. The proxy gives the reducer normal
 * indexed array operations while old folds retain their original tree root.
 * Public state materializes ordinary arrays at the core-state boundary.
 */

const BRANCH_FACTOR = 32;

type VectorNode<T> = readonly (VectorNode<T> | T | undefined)[];

interface Vector<T> {
  readonly root: VectorNode<T>;
  readonly depth: number;
  readonly length: number;
}

const vectors = new WeakMap<readonly unknown[], Vector<unknown>>();

/** Returns one entry without materializing the persistent array. */
export function runMapArrayAt<T>(values: readonly T[], index: number): T | undefined {
  const vector = vectorFor(values);
  return vector === undefined ? values[index] : vectorAt(vector, index);
}

/** Replaces one entry without copying the complete public array. */
export function replaceRunMapArrayEntry<T>(values: T[], index: number, value: T): T[] {
  if (index < 0 || index >= values.length) throw new RangeError(`Invalid run-map index ${index}`);
  if (runMapArrayAt(values, index) === value) return values;
  return arrayFor(vectorSet(vectorFor(values) ?? vectorFrom(values), index, value));
}

/** Appends one entry while preserving insertion order. */
export function appendRunMapArrayEntry<T>(values: T[], value: T): T[] {
  const vector = vectorFor(values) ?? vectorFrom(values);
  return arrayFor(vectorSet(vector, vector.length, value));
}

/** Sets a possibly sparse internal index entry. Public run-map arrays use dense indexes. */
export function setRunMapArrayEntry<T>(values: T[], index: number, value: T): T[] {
  if (!Number.isSafeInteger(index) || index < 0) {
    throw new RangeError(`Invalid run-map index ${index}`);
  }
  const vector = vectorFor(values) ?? vectorFrom(values);
  return arrayFor(vectorSet(vector, index, value));
}

/** Builds a persistent array after a whole-collection operation such as closeout. */
export function runMapArrayFrom<T>(values: readonly T[]): T[] {
  return arrayFor(vectorFrom(values));
}

/** Keeps an internal persistent array, or indexes a plain whole-history result once. */
export function ensureRunMapArray<T>(values: T[]): T[] {
  return vectorFor(values) === undefined ? runMapArrayFrom(values) : values;
}

function vectorFor<T>(values: readonly T[]): Vector<T> | undefined {
  return vectors.get(values) as Vector<T> | undefined;
}

function vectorFrom<T>(values: readonly T[]): Vector<T> {
  let vector: Vector<T> = {root: [], depth: 0, length: 0};
  for (const value of values) vector = vectorSet(vector, vector.length, value);
  return vector;
}

function vectorAt<T>(vector: Vector<T>, index: number): T | undefined {
  if (!Number.isSafeInteger(index) || index < 0 || index >= vector.length) return undefined;
  let node = vector.root;
  for (let level = vector.depth; level > 0; level -= 1) {
    const child = node[slotAt(index, level)];
    if (!Array.isArray(child)) return undefined;
    node = child as VectorNode<T>;
  }
  return node[slotAt(index, 0)] as T | undefined;
}

function vectorSet<T>(vector: Vector<T>, index: number, value: T): Vector<T> {
  let root = vector.root;
  let depth = vector.depth;
  while (index >= capacity(depth)) {
    root = [root];
    depth += 1;
  }
  return {
    root: setNode(root, depth, index, value),
    depth,
    length: Math.max(vector.length, index + 1),
  };
}

function setNode<T>(node: VectorNode<T>, level: number, index: number, value: T): VectorNode<T> {
  const next = [...node];
  const slot = slotAt(index, level);
  if (level === 0) {
    next[slot] = value;
    return next;
  }
  const child = node[slot];
  next[slot] = setNode(
    Array.isArray(child) ? (child as VectorNode<T>) : [],
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

function arrayFor<T>(vector: Vector<T>): T[] {
  // A real target length is required by native array consumers such as Bun's
  // partial matcher. Defining one accessor first keeps the target in sparse
  // dictionary form when its length grows; the persistent tree owns the data.
  const target: T[] = [];
  if (vector.length > 0) {
    Object.defineProperty(target, '0', {
      configurable: true,
      enumerable: true,
      get: () => vectorAt(vector, 0),
    });
    target.length = vector.length;
  }
  const proxy = new Proxy(target, {
    get(array, property, receiver) {
      const index = arrayIndex(property);
      return index === null ? Reflect.get(array, property, receiver) : vectorAt(vector, index);
    },
    has(array, property) {
      const index = arrayIndex(property);
      return index === null ? Reflect.has(array, property) : index < vector.length;
    },
    ownKeys(array) {
      const keys = Array.from({length: vector.length}, (_, index) => String(index));
      return [...keys, ...Reflect.ownKeys(array).filter(key => arrayIndex(key) === null)];
    },
    getOwnPropertyDescriptor(array, property) {
      const index = arrayIndex(property);
      if (index === null) return Reflect.getOwnPropertyDescriptor(array, property);
      if (index >= vector.length) return undefined;
      return {
        configurable: true,
        enumerable: true,
        value: vectorAt(vector, index),
        writable: false,
      };
    },
    set() {
      throw new TypeError('Run-map arrays are immutable');
    },
    defineProperty() {
      throw new TypeError('Run-map arrays are immutable');
    },
    deleteProperty() {
      throw new TypeError('Run-map arrays are immutable');
    },
    preventExtensions() {
      throw new TypeError('Run-map arrays are immutable');
    },
    setPrototypeOf() {
      throw new TypeError('Run-map arrays are immutable');
    },
  });
  vectors.set(proxy, vector as Vector<unknown>);
  return proxy;
}

function arrayIndex(property: string | symbol): number | null {
  if (typeof property !== 'string' || !/^(0|[1-9]\d*)$/.test(property)) return null;
  const index = Number(property);
  return Number.isSafeInteger(index) ? index : null;
}
