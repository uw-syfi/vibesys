/**
 * A project's tasks as the desktop reads them: `vibesys tasks PROJECT --json` prints one `TaskList`
 * (`src/entrypoints/tasks.py` is the authoritative definition). Parsing is strict: unknown keys and
 * wrong types are rejected, naming the path.
 */

export interface TaskList {
  /** The resolved project directory on the host. */
  readonly projectRoot: string;
  /** Task names, in the host's order (by name). */
  readonly tasks: readonly string[];
}

class TaskListError extends Error {
  override name = 'TaskListError';
}

const TASK = /^[a-z0-9][a-z0-9._-]{0,127}$/;

function object(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TaskListError(`${path} must be an object`);
  }
  const record = value as Record<string, unknown>;
  return record;
}

function onlyKeys(record: Record<string, unknown>, keys: readonly string[], path: string): void {
  for (const key of Object.keys(record)) {
    if (!keys.includes(key)) throw new TaskListError(`${path}.${key} is not a known field`);
  }
}

/** Parse a `vibesys tasks --json` document. */
export function parseTaskList(value: unknown): TaskList {
  const listing = object(value, 'tasks');
  onlyKeys(listing, ['version', 'project_root', 'tasks'], 'tasks');
  if (listing['version'] !== 1) throw new TaskListError('tasks.version must be 1');
  const projectRoot = listing['project_root'];
  if (typeof projectRoot !== 'string' || !projectRoot.startsWith('/')) {
    throw new TaskListError('tasks.project_root must be an absolute path');
  }
  const tasks = listing['tasks'];
  if (!Array.isArray(tasks)) throw new TaskListError('tasks.tasks must be a list');
  return {
    projectRoot,
    tasks: tasks.map((task, index) => {
      const path = `tasks.tasks[${index}]`;
      const record = object(task, path);
      onlyKeys(record, ['name'], path);
      const name = record['name'];
      if (typeof name !== 'string' || !TASK.test(name)) {
        throw new TaskListError(`${path}.name must be a task name`);
      }
      return name;
    }),
  };
}
