/**
 * The identifier formats the app accepts, from its views, its settings files, its command line, and
 * host output: one rule each, used everywhere that reads one.
 */

/** A live-instance (server process) id: 12 lowercase hex digits. */
export const INSTANCE_ID = /^[0-9a-f]{12}$/;

/** A task name, as `vs_project` defines it. */
export const TASK_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;

/** A run id: 1-128 lowercase letters, digits, `.`, `_`, `-`, starting with a letter or digit. */
export const RUN_ID = /^[a-z0-9][a-z0-9._-]{0,127}$/;

export function isInstanceId(value: unknown): value is string {
  return typeof value === 'string' && INSTANCE_ID.test(value);
}

export function isTaskName(value: unknown): value is string {
  return typeof value === 'string' && TASK_NAME.test(value);
}
