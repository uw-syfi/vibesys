/**
 * The detached-run registry as the desktop reads it: `vibesys --detach` prints one
 * `LiveInstanceRecord`, and `vibesys instances list --json` prints an `InstanceList`
 * (`src/server/instances.py` is the authoritative definition).
 *
 * Parsing is strict (unknown keys and wrong types are rejected, naming the path), with one
 * deliberate order: the record's `protocol_version` is read first. A server that speaks another
 * protocol version may also publish a record of another shape, so its record is reported as
 * `incompatible` with the two versions, never as a schema error the user cannot act on.
 */
import {PROTOCOL_VERSION} from '@vibesys/backend-client';

/** A live detached server this client can talk to. */
export interface LiveInstance {
  readonly id: string;
  readonly status: 'starting' | 'serving';
  readonly socketPath: string;
  readonly projectRoot: string;
  readonly runId: string | null;
  readonly pid: number;
  readonly startedAt: number;
  readonly hostname: string;
  readonly vibesysVersion: string;
}

/** What one record says: a server this client can read, or one that speaks another protocol. */
export type InstanceRecord =
  | {readonly kind: 'compatible'; readonly instance: LiveInstance}
  | {
      readonly kind: 'incompatible';
      readonly id: string | null;
      readonly protocolVersion: unknown;
      readonly vibesysVersion: string | null;
    };

export interface InstanceListing {
  readonly records: readonly InstanceRecord[];
  /** Registry ids whose liveness the host could not establish. */
  readonly unverified: readonly string[];
}

export class RecordError extends Error {
  override name = 'RecordError';
}

const RECORD_KEYS = [
  'version',
  'id',
  'status',
  'socket_path',
  'project_root',
  'run_id',
  'pid',
  'started_at',
  'hostname',
  'protocol_version',
  'vibesys_version',
] as const;
const INSTANCE_ID = /^[0-9a-f]{12}$/;

/** Parse one `LiveInstanceRecord` document; `path` names it in errors. */
export function parseInstanceRecord(value: unknown, path = 'record'): InstanceRecord {
  const record = object(value, path);
  const protocolVersion = record['protocol_version'];
  if (protocolVersion !== PROTOCOL_VERSION) {
    const id = record['id'];
    const vibesysVersion = record['vibesys_version'];
    return {
      kind: 'incompatible',
      id: typeof id === 'string' && INSTANCE_ID.test(id) ? id : null,
      protocolVersion,
      vibesysVersion: typeof vibesysVersion === 'string' ? vibesysVersion : null,
    };
  }
  onlyKeys(record, RECORD_KEYS, path);
  if (record['version'] !== 1) throw new RecordError(`${path}.version must be 1`);
  const id = string(record, 'id', path);
  if (!INSTANCE_ID.test(id)) throw new RecordError(`${path}.id must be 12 hex digits`);
  const status = record['status'];
  if (status !== 'starting' && status !== 'serving') {
    throw new RecordError(`${path}.status must be "starting" or "serving"`);
  }
  const runId = record['run_id'] ?? null;
  if (runId !== null && typeof runId !== 'string') {
    throw new RecordError(`${path}.run_id must be a string or null`);
  }
  const pid = number(record, 'pid', path);
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new RecordError(`${path}.pid must be a positive integer`);
  }
  return {
    kind: 'compatible',
    instance: {
      id,
      status,
      socketPath: string(record, 'socket_path', path),
      projectRoot: string(record, 'project_root', path),
      runId,
      pid,
      startedAt: number(record, 'started_at', path),
      hostname: string(record, 'hostname', path),
      vibesysVersion: string(record, 'vibesys_version', path),
    },
  };
}

/** Parse a `vibesys instances list --json` document. */
export function parseInstanceList(value: unknown): InstanceListing {
  const listing = object(value, 'listing');
  onlyKeys(listing, ['version', 'instances', 'unverified'], 'listing');
  if (listing['version'] !== 1) throw new RecordError('listing.version must be 1');
  const instances = listing['instances'];
  if (!Array.isArray(instances)) throw new RecordError('listing.instances must be a list');
  const unverified = listing['unverified'] ?? [];
  if (!Array.isArray(unverified) || !unverified.every(id => typeof id === 'string')) {
    throw new RecordError('listing.unverified must be a list of ids');
  }
  return {
    records: instances.map((record, index) =>
      parseInstanceRecord(record, `listing.instances[${index}]`),
    ),
    unverified,
  };
}

/** The message for a server this client cannot read: both versions, and the command to change. */
export function versionSkewMessage(skew: {
  readonly hostName: string;
  readonly vibesysCommand: string;
  readonly protocolVersion: unknown;
  readonly vibesysVersion: string | null;
}): string {
  const theirs = skew.vibesysVersion === null ? '' : ` (VibeSys ${skew.vibesysVersion})`;
  return (
    `This app speaks VibeSys protocol version ${PROTOCOL_VERSION}, but the server on ` +
    `${skew.hostName}${theirs} speaks protocol version ${String(skew.protocolVersion)}. ` +
    `Update the app or the VibeSys that "${skew.vibesysCommand}" runs on ${skew.hostName} ` +
    'so that they match.'
  );
}

function object(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new RecordError(`${path} must be an object`);
  }
  return value as Record<string, unknown>;
}

function onlyKeys(record: Record<string, unknown>, allowed: readonly string[], path: string) {
  for (const key of Object.keys(record)) {
    if (!allowed.includes(key)) throw new RecordError(`${path}.${key} is not a known field`);
  }
}

function string(record: Record<string, unknown>, key: string, path: string): string {
  const value = record[key];
  if (typeof value !== 'string') throw new RecordError(`${path}.${key} must be a string`);
  return value;
}

function number(record: Record<string, unknown>, key: string, path: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new RecordError(`${path}.${key} must be a number`);
  }
  return value;
}
