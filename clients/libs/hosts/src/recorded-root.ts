/**
 * A project root taken from a run's recorded facts. A path the operator typed
 * (`~/x`, a relative path) is not one: the host records the canonical absolute
 * directory, and controls exist only for that value.
 */

declare const recordedProjectRootBrand: unique symbol;

/** An absolute directory from a run record. */
export type RecordedProjectRoot = string & {readonly [recordedProjectRootBrand]: true};

/** The recorded root `value` names, or undefined when it is not an absolute directory. */
export function recordedProjectRoot(value: string | undefined): RecordedProjectRoot | undefined {
  if (value === undefined || !value.startsWith('/')) return undefined;
  return value as RecordedProjectRoot;
}
