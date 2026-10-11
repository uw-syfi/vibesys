/**
 * The identifier formats the app accepts, from its views, its settings files, its command line, and
 * host output: one predicate each, used everywhere that reads one.
 */

/** A live-instance (server process) id: 12 lowercase hex digits. */
const INSTANCE_ID = /^[0-9a-f]{12}$/;

/**
 * The name grammar `vs_project` gives task names and run ids alike: 1-128 lowercase letters,
 * digits, `.`, `_`, `-`, starting with a letter or digit.
 */
const NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;

export function isInstanceId(value: unknown): value is string {
  return typeof value === 'string' && INSTANCE_ID.test(value);
}

export function isTaskName(value: unknown): value is string {
  return typeof value === 'string' && NAME.test(value);
}

export function isRunId(value: unknown): value is string {
  return typeof value === 'string' && NAME.test(value);
}
