/**
 * The runs the user opened (`recent.json` in the app's user data), as pure, validated data. The
 * welcome view lists them for one-click reattach and resume. An entry names its run by
 * `(host, runId)`, recorded from the run's record when the app starts or attaches to it, never
 * later and never guessed; its status comes from the host's registry, never from this file.
 */
import {type HostKey, hostFromKey, validHostPath} from '../../../common/hosts.js';
import {isRunId, isTaskName} from '../../../common/ids.js';
import {object, onlyKeys, SettingsError} from './parse.js';

/** One run the user opened. */
export interface RecentRun {
  readonly host: HostKey;
  /** The run's project directory on the host. */
  readonly project: string;
  /** The task it was started with, when the app started it; null when only attached to. */
  readonly task: string | null;
  readonly runId: string;
  /** When the user last opened it, in milliseconds since the epoch. */
  readonly attachedAt: number;
}

export interface RecentFile {
  readonly version: 2;
  readonly runs: readonly RecentRun[];
}

export const EMPTY_RECENT: RecentFile = {version: 2, runs: []};
/** How many runs the list keeps. */
const RECENT_LIMIT = 20;

const KEYS = ['host', 'project', 'task', 'runId', 'attachedAt'] as const;
/** Version 1 also kept the server's instance id, and a run id only once its server reported one. */
const V1_KEYS = [...KEYS, 'instanceId'] as const;

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
function parseRun(value: unknown, path: string, keys: readonly string[]): RecentRun {
  const record = object(value, path);
  onlyKeys(record, keys, path);
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
    attachedAt,
  };
}

/**
 * Validate a whole `recent.json` document. A version 1 file migrates: an entry saved before its
 * run's id was known names no run, so it is dropped rather than guessed.
 */
export function parseRecent(value: unknown): RecentFile {
  const record = object(value, 'recent.json');
  onlyKeys(record, ['version', 'runs'], 'recent.json');
  const version = record['version'];
  if (version !== 1 && version !== 2) throw new SettingsError('recent.json: version must be 2');
  const runs = record['runs'];
  if (!Array.isArray(runs)) throw new SettingsError('recent.json: runs must be a list');
  const kept =
    version === 1 ? runs.filter(run => object(run, 'recent.json: runs[]')['runId'] != null) : runs;
  const keys = version === 1 ? V1_KEYS : KEYS;
  return {
    version: 2,
    runs: kept.map((run, index) => parseRun(run, `recent.json: runs[${index}]`, keys)),
  };
}

function sameRun(a: RecentRun, b: RecentRun): boolean {
  return a.host === b.host && a.runId === b.runId;
}

/**
 * `file` with `run` first: an entry for the same run is replaced (keeping its task when `run`
 * does not know one), and the list keeps the `limit` most recent runs.
 */
export function remember(file: RecentFile, run: RecentRun, limit = RECENT_LIMIT): RecentFile {
  const previous = file.runs.find(entry => sameRun(entry, run));
  const merged: RecentRun = {...run, task: run.task ?? previous?.task ?? null};
  const rest = file.runs.filter(entry => !sameRun(entry, run));
  return {version: 2, runs: [merged, ...rest].slice(0, Math.max(0, limit))};
}
