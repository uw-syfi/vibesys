import {type HostKey, hostFromKey} from '@vibesys/hosts';
import {isRunId} from './ids.js';

/** A run named by the host it ran on and its run id: what the app's own records keep of it. */
export interface RunRef {
  readonly host: HostKey;
  readonly runId: string;
}

/** Stable, unambiguous key for one run across every host the desktop knows. */
export function runRefKey(run: RunRef): string {
  return JSON.stringify([run.host, run.runId]);
}

/** Whether `left` and `right` name the same run. */
export function sameRun(left: RunRef, right: RunRef): boolean {
  return left.host === right.host && left.runId === right.runId;
}

/** `value` as a `RunRef`, or an `Error` naming the offending key under `path`. */
export function parseRunRef(value: unknown, path: string): RunRef {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${path} must be an object`);
  }
  const record = value as Record<string, unknown>;
  const unknown = Object.keys(record).find(key => key !== 'host' && key !== 'runId');
  if (unknown !== undefined) throw new Error(`${path}.${unknown} is not a known field`);
  try {
    hostFromKey(record['host']);
  } catch (error) {
    throw new Error(`${path}.host: ${(error as Error).message}`);
  }
  if (!isRunId(record['runId'])) throw new Error(`${path}.runId must be a run id`);
  return {host: record['host'] as HostKey, runId: record['runId']};
}
