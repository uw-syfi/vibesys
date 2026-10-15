/**
 * The runs the user opened (`recent.json` in the app's user data), as pure, validated data. The
 * welcome view lists them for one-click reattach and resume. An entry names its run by
 * `(host, project, runId)`, recorded from the run's record when the app starts or attaches to it,
 * never later and never guessed. Run IDs are project-scoped. The latest attached or inspected
 * server id is retained only as the locator for ID-scoped diagnostics after registry tombstones
 * expire; status still comes from the host's registry, never from this file.
 */
import {type HostKey, hostFromKey, validHostPath} from '@vibesys/hosts';
import {isInstanceId, isRunId, isTaskName, recentRunKey} from '@vibesys/runs';
import {object, onlyKeys, SettingsError} from './parse.js';

/** One run the user opened, as `recent.json` stores it (the welcome view lists `RecentRun`). */
export interface StoredRecentRun {
  readonly host: HostKey;
  /** The run's project directory on the host. */
  readonly project: string;
  /** The task it was started with, when the app started it; null when only attached to. */
  readonly task: string | null;
  readonly runId: string;
  /** The latest server instance attached to or inspected for this run. */
  readonly instanceId: string | null;
  /** When the user last opened it, in milliseconds since the epoch. */
  readonly attachedAt: number;
}

export interface RecentFile {
  readonly version: 3;
  readonly runs: readonly StoredRecentRun[];
}

export const EMPTY_RECENT: RecentFile = {version: 3, runs: []};
/** How many runs the list keeps. */
const RECENT_LIMIT = 20;

const V2_KEYS = ['host', 'project', 'task', 'runId', 'attachedAt'] as const;
/** Version 1 also kept the server's instance id, and a run id only once its server reported one. */
const KEYS = [...V2_KEYS, 'instanceId'] as const;

function matching(
  value: unknown,
  valid: (value: unknown) => value is string,
  what: string,
  path: string,
): string {
  if (!valid(value)) throw new SettingsError(`${path} must be ${what}`);
  return value;
}

/** Validate one entry, rejecting unknown keys. */
function parseRun(value: unknown, path: string, version: 1 | 2 | 3): StoredRecentRun {
  const record = object(value, path);
  onlyKeys(record, version === 2 ? V2_KEYS : KEYS, path);
  const host = record['host'];
  try {
    hostFromKey(host);
  } catch (error) {
    throw new SettingsError(`${path}.host: ${(error as Error).message}`);
  }
  const attachedAt = record['attachedAt'];
  if (typeof attachedAt !== 'number' || !Number.isFinite(attachedAt) || attachedAt < 0) {
    throw new SettingsError(`${path}.attachedAt must be a non-negative number`);
  }
  const task =
    record['task'] === null
      ? null
      : matching(record['task'], isTaskName, 'a task name', `${path}.task`);
  return {
    host: host as HostKey,
    project: validHostPath(record['project'], `${path}.project`),
    task,
    runId: matching(record['runId'], isRunId, 'a run id', `${path}.runId`),
    instanceId:
      version === 2 || record['instanceId'] === null
        ? null
        : matching(
            record['instanceId'],
            isInstanceId,
            'a 12-digit instance id',
            `${path}.instanceId`,
          ),
    attachedAt,
  };
}

/**
 * Validate a whole `recent.json` document. Version 1 keeps its instance locator but drops an entry
 * saved before its run id was known. Version 2 predates durable locators, so it migrates with a
 * null locator until the run is next attached or inspected.
 */
export function parseRecent(value: unknown): RecentFile {
  const record = object(value, 'recent.json');
  onlyKeys(record, ['version', 'runs'], 'recent.json');
  const version = record['version'];
  if (version !== 1 && version !== 2 && version !== 3) {
    throw new SettingsError('recent.json: version must be 3');
  }
  const runs = record['runs'];
  if (!Array.isArray(runs)) throw new SettingsError('recent.json: runs must be a list');
  const kept =
    version === 1 ? runs.filter(run => object(run, 'recent.json: runs[]')['runId'] != null) : runs;
  return {
    version: 3,
    runs: kept.map((run, index) => parseRun(run, `recent.json: runs[${index}]`, version)),
  };
}

function sameRun(a: StoredRecentRun, b: StoredRecentRun): boolean {
  return recentRunKey(a) === recentRunKey(b);
}

/**
 * `file` with `run` first: an entry for the same run is replaced (keeping its task when `run`
 * does not know one), and the list keeps the `limit` most recent runs.
 */
export function remember(file: RecentFile, run: StoredRecentRun, limit = RECENT_LIMIT): RecentFile {
  const previous = file.runs.find(entry => sameRun(entry, run));
  const merged: StoredRecentRun = {
    ...run,
    task: run.task ?? previous?.task ?? null,
    instanceId: run.instanceId ?? previous?.instanceId ?? null,
  };
  const rest = file.runs.filter(entry => !sameRun(entry, run));
  return {version: 3, runs: [merged, ...rest].slice(0, Math.max(0, limit))};
}

export interface RecentInstance {
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
  readonly instanceId: string;
}

/** Preserve a newly inspected instance for an existing recent run without reordering it. */
export function rememberInstance(file: RecentFile, inspected: RecentInstance): RecentFile {
  return {
    version: 3,
    runs: file.runs.map(run =>
      recentRunKey(run) === recentRunKey(inspected)
        ? {...run, instanceId: inspected.instanceId}
        : run,
    ),
  };
}
