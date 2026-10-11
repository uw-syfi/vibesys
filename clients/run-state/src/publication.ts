/** Build-aware publication guards. Kept runtime-neutral for browser consumers. */
type JsonPrimitive = boolean | null | number | string;

/** Keeps Array.isArray's mutable type predicate from exposing mutators. */
interface ReadonlyArrayMutationBarrier {
  readonly copyWithin?: never;
  readonly fill?: never;
  readonly pop?: never;
  readonly push?: never;
  readonly reverse?: never;
  readonly shift?: never;
  readonly sort?: never;
  readonly splice?: never;
  readonly unshift?: never;
}

type ReadonlyArrayProjection<T> = readonly T[] & ReadonlyArrayMutationBarrier;

interface ReadonlyJsonObject {
  readonly [key: string]: ReadonlyJsonValue;
}

type ReadonlyJsonArray = readonly ReadonlyJsonValue[] & ReadonlyArrayMutationBarrier;

type ReadonlyJsonComposite = ReadonlyJsonArray | ReadonlyJsonObject;

type ReadonlyJsonValue = JsonPrimitive | ReadonlyJsonObject | ReadonlyJsonArray;

declare global {
  interface ArrayConstructor {
    /**
     * TypeScript's built-in predicate widens read-only arrays to mutable `any[]`.
     * This earlier, narrower overload applies only to published JSON composites;
     * mutable arrays and generated protocol inputs still use the built-in overload.
     */
    isArray(value: ReadonlyJsonComposite): value is ReadonlyJsonArray;
  }
}

/** Deeply read-only view of a value received through the JSON protocol. */
export type ReadonlyProjection<T> = unknown extends T
  ? ReadonlyJsonValue
  : T extends JsonPrimitive
    ? T
    : T extends readonly (infer Item)[]
      ? ReadonlyArrayProjection<ReadonlyProjection<Item>>
      : T extends object
        ? {readonly [Key in keyof T]: ReadonlyProjection<T[Key]>}
        : T;

function developmentPublicationGuardsEnabled(): boolean {
  const vite = (import.meta as ImportMeta & {env?: {DEV?: boolean}}).env;
  if (vite?.DEV !== undefined) return vite.DEV === true;
  const runtime = globalThis as typeof globalThis & {
    process?: {env?: {NODE_ENV?: string}};
  };
  return runtime.process?.env?.NODE_ENV === 'development';
}

/**
 * Publishes one projection-owned value as a recursively frozen development value.
 * The traversal is cycle-safe. Production returns without walking the value.
 */
export function publishProjectionValue<T>(value: T): T {
  if (developmentPublicationGuardsEnabled()) freezeReachable(value, new WeakSet());
  return value;
}

/**
 * Publishes every eager, consumer-visible reference on a projection object.
 * Accessors are left lazy and must publish their value when first evaluated.
 */
export function publishProjectionFields<T extends object>(projection: T): T {
  if (!developmentPublicationGuardsEnabled()) return projection;
  const seen = new WeakSet<object>();
  for (const property of Object.keys(projection)) {
    const descriptor = Object.getOwnPropertyDescriptor(projection, property);
    if (descriptor === undefined || !('value' in descriptor)) continue;
    freezeReachable(descriptor.value, seen);
  }
  return projection;
}

/** Owns a caller-supplied protocol value before the development guard freezes it. */
export function ownProjectionInput<T>(value: T): ReadonlyProjection<T> {
  return (
    developmentPublicationGuardsEnabled() ? cloneReachable(value, new WeakMap()) : value
  ) as ReadonlyProjection<T>;
}

function freezeReachable(value: unknown, seen: WeakSet<object>): void {
  if (typeof value !== 'object' || value === null || seen.has(value)) return;
  seen.add(value);
  for (const property of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, property);
    if (descriptor !== undefined && 'value' in descriptor) freezeReachable(descriptor.value, seen);
  }
  Object.freeze(value);
}

function cloneReachable(value: unknown, copies: WeakMap<object, object>): unknown {
  if (typeof value !== 'object' || value === null) return value;
  const existing = copies.get(value);
  if (existing !== undefined) return existing;

  const copy: object = Array.isArray(value) ? [] : Object.create(Object.getPrototypeOf(value));
  copies.set(value, copy);
  for (const property of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, property);
    if (descriptor === undefined) continue;
    Object.defineProperty(
      copy,
      property,
      'value' in descriptor
        ? {...descriptor, value: cloneReachable(descriptor.value, copies)}
        : descriptor,
    );
  }
  return copy;
}
