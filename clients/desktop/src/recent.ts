/**
 * The runs the user attached to before, as pure, validated data (`recent.json` in the app's user
 * data). The welcome view lists them for one-click reattach; each entry's status is refreshed from
 * its host's live registry, never stored: a run is live only while a registry record proves it.
 */
import {type HostKey, hostFromKey, validHostPath} from './host-settings.js';
import type {InstanceRecord} from './instances.js';

/** One run the user attached to. */
export interface RecentRun {
  readonly host: HostKey;
  /** The run's project directory on the host. */
  readonly project: string;
  /** The task it was started with, when the app started it; null when only attached to. */
  readonly task: string | null;
  /** The registry id of the server last seen driving it. */
  readonly instanceId: string;
  /** The run's id, once its server reported one. */
  readonly runId: string | null;
  /** When the user last attached, in milliseconds since the epoch. */
  readonly attachedAt: number;
}

export interface RecentFile {
  readonly version: 1;
  readonly runs: readonly RecentRun[];
}

export const EMPTY_RECENT: RecentFile = {version: 1, runs: []};
/** How many runs the list keeps. */
export const RECENT_LIMIT = 20;

class RecentError extends Error {
  override name = 'RecentError';
}

const INSTANCE_ID = /^[0-9a-f]{12}$/;
const RUN_ID = /^[A-Za-z0-9._-]+$/;
const TASK = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const KEYS = ['host', 'project', 'task', 'instanceId', 'runId', 'attachedAt'] as const;

function nullable(value: unknown, pattern: RegExp, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new RecentError(`${path} must be null or match ${pattern.source}`);
  }
  return value;
}

/** Validate one entry, rejecting unknown keys. */
function parseRun(value: unknown, path: string): RecentRun {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new RecentError(`${path} must be an object`);
  }
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!(KEYS as readonly string[]).includes(key)) {
      throw new RecentError(`${path}.${key} is not a known field`);
    }
  }
  const host = record['host'];
  try {
    hostFromKey(host);
  } catch (error) {
    throw new RecentError(`${path}.host: ${(error as Error).message}`);
  }
  const instanceId = record['instanceId'];
  if (typeof instanceId !== 'string' || !INSTANCE_ID.test(instanceId)) {
    throw new RecentError(`${path}.instanceId must be 12 hex digits`);
  }
  const attachedAt = record['attachedAt'];
  if (typeof attachedAt !== 'number' || !Number.isFinite(attachedAt) || attachedAt < 0) {
    throw new RecentError(`${path}.attachedAt must be a non-negative number`);
  }
  let project: string;
  try {
    project = validHostPath(record['project'], `${path}.project`);
  } catch (error) {
    throw new RecentError((error as Error).message);
  }
  return {
    host: host as HostKey,
    project,
    task: nullable(record['task'], TASK, `${path}.task`),
    instanceId,
    runId: nullable(record['runId'], RUN_ID, `${path}.runId`),
    attachedAt,
  };
}

/** Validate a whole `recent.json` document. */
export function parseRecent(value: unknown): RecentFile {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new RecentError('recent.json must be an object');
  }
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (key !== 'version' && key !== 'runs') {
      throw new RecentError(`recent.json: ${key} is not a known field`);
    }
  }
  if (record['version'] !== 1) throw new RecentError('recent.json: version must be 1');
  const runs = record['runs'];
  if (!Array.isArray(runs)) throw new RecentError('recent.json: runs must be a list');
  return {version: 1, runs: runs.map((run, index) => parseRun(run, `recent.json: runs[${index}]`))};
}

/** True when `a` and `b` are the same run: the same server, or the same run id on one host. */
function sameRun(a: RecentRun, b: RecentRun): boolean {
  if (a.host !== b.host) return false;
  return a.instanceId === b.instanceId || (a.runId !== null && a.runId === b.runId);
}

/**
 * `file` with `run` first: an entry for the same run is replaced (keeping its task when `run`
 * does not know one), and the list keeps the `limit` most recent runs.
 */
export function remember(file: RecentFile, run: RecentRun, limit = RECENT_LIMIT): RecentFile {
  const previous = file.runs.find(entry => sameRun(entry, run));
  const merged: RecentRun = {
    ...run,
    task: run.task ?? previous?.task ?? null,
    runId: run.runId ?? (previous?.instanceId === run.instanceId ? previous.runId : null),
  };
  const rest = file.runs.filter(entry => !sameRun(entry, run));
  return {version: 1, runs: [merged, ...rest].slice(0, Math.max(0, limit))};
}

/** What the welcome view says about a recent run after asking its host. */
export type RecentStatus =
  /** A live server drives it; attach to `instanceId`. */
  | {readonly kind: 'live'; readonly instanceId: string; readonly status: 'starting' | 'serving'}
  /** No live server drives it: it finished, stopped, or crashed. */
  | {readonly kind: 'ended'}
  /** The host could not be asked (`detail` says why). */
  | {readonly kind: 'unknown'; readonly detail: string};

/**
 * `run`'s status from its host's live records (or why they could not be read): live when a
 * record is its server, or drives the same run id in the same project (a resumed run).
 */
export function recentStatus(
  run: RecentRun,
  records: readonly InstanceRecord[] | {readonly error: string},
): RecentStatus {
  if (!Array.isArray(records)) return {kind: 'unknown', detail: (records as {error: string}).error};
  for (const record of records as readonly InstanceRecord[]) {
    if (record.kind !== 'compatible') continue;
    const {instance} = record;
    const sameServer = instance.id === run.instanceId;
    const resumed =
      run.runId !== null && instance.runId === run.runId && instance.projectRoot === run.project;
    if (sameServer || resumed) {
      return {kind: 'live', instanceId: instance.id, status: instance.status};
    }
  }
  return {kind: 'ended'};
}
