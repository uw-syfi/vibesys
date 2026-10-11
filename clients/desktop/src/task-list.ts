/**
 * A project's tasks as the desktop reads them: `vibesys tasks PROJECT --json` prints one `TaskList`
 * (`src/entrypoints/tasks.py` is the authoritative definition). Parsing is strict: unknown keys and
 * wrong types are rejected, naming the path.
 */
import {isTaskName} from './ids.js';

export interface TaskList {
  /** The resolved project directory on the host. */
  readonly projectRoot: string;
  /** Task names, in the host's order (by name). */
  readonly tasks: readonly string[];
}

class TaskListError extends Error {
  override name = 'TaskListError';
}

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
      if (!isTaskName(name)) {
        throw new TaskListError(`${path}.name must be a task name`);
      }
      return name;
    }),
  };
}

/** What failed while listing a project's tasks, as the host reported it. */
export type TaskListFailure =
  /** `vibesys tasks` exited unsuccessfully; `stderr` is what it printed. */
  | {readonly kind: 'exited'; readonly stderr: string}
  /** It printed something that is not a task list this app reads. */
  | {readonly kind: 'unreadable'};

/**
 * The sentence the start-run sheet shows when `project`'s tasks could not be listed: plain
 * language, never a command line or a usage dump.
 */
export function taskListProblem(project: string, failure: TaskListFailure): string {
  if (failure.kind === 'unreadable') {
    return 'The VibeSys checkout on this host answered with a task list this app cannot read. Update the checkout or the app.';
  }
  const said = /^vibesys tasks: (.+)$/m.exec(failure.stderr)?.[1]?.trim();
  if (said !== undefined) return `Could not read the tasks of ${project}: ${said}`;
  if (/usage:|invalid choice|unrecognized arguments/i.test(failure.stderr)) {
    return 'The VibeSys checkout on this host is too old to list tasks. Update it, then try again.';
  }
  return `Could not read the tasks of ${project}. The host's VibeSys reported an error; the app log has its output.`;
}
