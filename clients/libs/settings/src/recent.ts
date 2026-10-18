/**
 * The runs the user opened (`recent.json` in the app's user data), as pure, validated data. The
 * welcome view lists them for one-click reattach and resume. An entry names its run by
 * `(host, project, runId)`, recorded from the run's record when the app starts or attaches to it,
 * never later and never guessed. Run IDs are project-scoped. The latest attached or inspected
 * server id is retained only as the locator for ID-scoped diagnostics after registry tombstones
 * expire; status still comes from the host's registry, never from this file.
 */
import {type HostKey, hostFromKey, validHostPath} from '@vibesys/hosts';
import {
  isInstanceId,
  isRunId,
  isTaskName,
  parseStoredRunStart,
  type RunStart,
  recentRunKey,
  type SkippedRecent,
} from '@vibesys/runs';
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
  /** What the app started the run with, so Run again can start from it; absent when it did not. */
  readonly start?: RunStart;
}

export interface RecentFile {
  readonly version: 4;
  readonly runs: readonly StoredRecentRun[];
}

export const EMPTY_RECENT: RecentFile = {version: 4, runs: []};
/** How many runs the list keeps. */
const RECENT_LIMIT = 20;

const V2_KEYS = ['host', 'project', 'task', 'runId', 'attachedAt'] as const;
/** Version 1 also kept the server's instance id, and a run id only once its server reported one. */
const V3_KEYS = [...V2_KEYS, 'instanceId'] as const;
/** Version 4 also keeps what the app started the run with. */
const KEYS = [...V3_KEYS, 'start'] as const;

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
function parseRun(value: unknown, path: string, version: 1 | 2 | 3 | 4): StoredRecentRun {
  const record = object(value, path);
  onlyKeys(record, version === 2 ? V2_KEYS : version === 4 ? KEYS : V3_KEYS, path);
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
  const start = version === 4 ? parseStart(record['start'], `${path}.start`) : null;
  const task =
    record['task'] === null
      ? null
      : matching(record['task'], isTaskName, 'a task name', `${path}.task`);
  let project: string;
  try {
    project = validHostPath(record['project'], `${path}.project`);
  } catch (error) {
    throw new SettingsError((error as Error).message);
  }
  return {
    host: host as HostKey,
    project,
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
    ...(start === null ? {} : {start}),
  };
}

function parseStart(value: unknown, path: string): RunStart | null {
  if (value === null || value === undefined) return null;
  try {
    return parseStoredRunStart(value);
  } catch (error) {
    throw new SettingsError(`${path}: ${(error as Error).message}`);
  }
}

/** An entry of `recent.json` that could not be read, kept as the file holds it. */
export interface InvalidRecentEntry {
  /** The entry's JSON value, exactly as parsed; written back unchanged. */
  readonly raw: unknown;
  /** How many valid entries precede it in the file. */
  readonly after: number;
}

/** What `recent.json` holds: the runs it lists validly, and the entries it could not read. */
export interface ParsedRecent {
  readonly file: RecentFile;
  /** Every entry left out, in file order; none is ever repaired or guessed. */
  readonly skipped: readonly SkippedRecent[];
  /** The same entries with their values, so a write can keep them. */
  readonly invalid: readonly InvalidRecentEntry[];
}

/**
 * Validate a whole `recent.json` document. The document itself must be valid: its keys, version and
 * list. Each entry is validated strictly on its own, and an invalid one is skipped and reported by
 * its place and reason, so one bad entry does not hide the others. Version 1 keeps its instance
 * locator but drops an entry saved before its run id was known, without a report. Version 2
 * predates durable locators, so it migrates with a null locator until the run is next attached or
 * inspected.
 */
export function parseRecent(value: unknown): ParsedRecent {
  const record = object(value, 'recent.json');
  onlyKeys(record, ['version', 'runs'], 'recent.json');
  const version = record['version'];
  if (version !== 1 && version !== 2 && version !== 3 && version !== 4) {
    throw new SettingsError('recent.json: version must be 4');
  }
  const runs = record['runs'];
  if (!Array.isArray(runs)) throw new SettingsError('recent.json: runs must be a list');
  const kept: StoredRecentRun[] = [];
  const skipped: SkippedRecent[] = [];
  const invalid: InvalidRecentEntry[] = [];
  for (const [index, entry] of runs.entries()) {
    const path = `recent.json: runs[${index}]`;
    try {
      if (version === 1 && object(entry, path)['runId'] == null) continue;
      kept.push(parseRun(entry, path, version));
    } catch (error) {
      if (!(error instanceof SettingsError)) throw error;
      skipped.push({index, reason: error.message});
      invalid.push({raw: entry, after: kept.length});
    }
  }
  return {file: {version: 4, runs: kept}, skipped, invalid};
}

/**
 * The document to write for `file` while `invalid` entries stay in it: each keeps its value and its
 * place after the same number of valid entries (or at the end when fewer remain), so a write never
 * drops or repairs an entry the user has to fix.
 */
export function recentDocument(
  file: RecentFile,
  invalid: readonly InvalidRecentEntry[],
): {readonly version: 4; readonly runs: readonly unknown[]} {
  const runs: unknown[] = [];
  let next = 0;
  for (let placed = 0; placed <= file.runs.length; placed++) {
    while (
      next < invalid.length &&
      Math.min(invalid[next]?.after ?? 0, file.runs.length) <= placed
    ) {
      runs.push(invalid[next]?.raw);
      next++;
    }
    const run = file.runs[placed];
    if (run !== undefined) runs.push(run);
  }
  return {version: 4, runs};
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
  const start = run.start ?? previous?.start;
  const merged: StoredRecentRun = {
    ...run,
    task: run.task ?? previous?.task ?? null,
    instanceId: run.instanceId ?? previous?.instanceId ?? null,
    ...(start === undefined ? {} : {start}),
  };
  const rest = file.runs.filter(entry => !sameRun(entry, run));
  return {version: 4, runs: [merged, ...rest].slice(0, Math.max(0, limit))};
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
    version: 4,
    runs: file.runs.map(run =>
      recentRunKey(run) === recentRunKey(inspected)
        ? {...run, instanceId: inspected.instanceId}
        : run,
    ),
  };
}
