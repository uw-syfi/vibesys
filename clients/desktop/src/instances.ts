/**
 * The detached-run registry as the desktop reads it: `vibesys --detach` prints one
 * `LiveInstanceRecord` (or one `DetachedLaunchFailure`), `vibesys instances list --json` prints an
 * `InstanceList`, and `vibesys instances stop ID --json` an `InstanceStopResult`
 * (`src/server/host_api.py` is the authoritative definition).
 *
 * Parsing is strict (unknown keys and wrong types are rejected, naming the path), with one
 * deliberate order: the record's `protocol_version` is read first. A server that speaks another
 * protocol version may also publish a record of another shape, so its record is reported as
 * `incompatible` with the two versions, never as a schema error the user cannot act on.
 */
import {PROTOCOL_VERSION} from '@vibesys/backend-client';
import {isInstanceId} from './ids.js';

/** A live detached server this client can talk to. */
interface LiveInstance {
  readonly id: string;
  readonly status: 'starting' | 'serving';
  readonly socketPath: string;
  readonly projectRoot: string;
  readonly runId: string | null;
  readonly pid: number;
  readonly startedAt: number;
  readonly hostname: string;
  readonly vibesysVersion: string;
  /** The VibeSys checkout the server runs from; null for an install, or a server too old to say. */
  readonly vibesysRoot: string | null;
  /** The run's lifecycle phase (record version 3 and later); null from an older host. */
  readonly phase: RunPhase | null;
  /** When the server exited, for an ended record; null while it lives. */
  readonly endedAt: number | null;
}

const RUN_STATUSES = [
  'starting',
  'running',
  'pausing',
  'paused',
  'stopping',
  'stopped',
  'completed',
  'failed',
] as const;

/**
 * `RunPhase` in `src/server/host_api.py`: the run's status with its stage (and, from record
 * version 4, the stage's progress) or failure reason.
 */
interface RunPhase {
  readonly status: (typeof RUN_STATUSES)[number];
  readonly stage: string | null;
  readonly progress: StartupProgress | null;
  readonly reason: {readonly code: string; readonly stage: string; readonly message: string} | null;
}

/** `StartupProgress` in `src/server/host_api.py`: `done` of `total` steps, now on `label`. */
interface StartupProgress {
  readonly done: number;
  readonly total: number | null;
  readonly label: string;
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
  /** Servers that exited in the last day, with the phase their run ended in; absent before version 3. */
  readonly ended?: readonly InstanceRecord[];
  /** Registry ids whose liveness the host could not establish. */
  readonly unverified: readonly string[];
  /** Live servers whose record the host could not read (another version, or corrupt); absent from an older host. */
  readonly unreadable?: readonly {readonly id: string; readonly version: number | null}[];
}

/** What the registry established about one id, in decreasing order of authority. */
export type InstanceObservation =
  | InstanceRecord
  | {readonly kind: 'unverified'}
  | {readonly kind: 'absent'};

/**
 * The registry's observation for `id`. A compatible record is authoritative even when an
 * overlapping listing also calls the id unverified. Without one, unverified wins over an incompatible record:
 * neither the incompatible record nor its absence proves that the run ended.
 */
export function instanceObservation(listing: InstanceListing, id: string): InstanceObservation {
  const compatible = listing.records.find(
    record => record.kind === 'compatible' && record.instance.id === id,
  );
  if (compatible !== undefined) return compatible;
  if (listing.unverified.includes(id)) return {kind: 'unverified'};
  return (
    listing.records.find(record => record.kind === 'incompatible' && record.id === id) ?? {
      kind: 'absent',
    }
  );
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
  'vibesys_root',
] as const;

/** Parse one `LiveInstanceRecord` document; `path` names it in errors. */
export function parseInstanceRecord(value: unknown, path = 'record'): InstanceRecord {
  const record = object(value, path);
  const protocolVersion = record['protocol_version'];
  if (protocolVersion !== PROTOCOL_VERSION) {
    const id = record['id'];
    const vibesysVersion = record['vibesys_version'];
    return {
      kind: 'incompatible',
      id: isInstanceId(id) ? id : null,
      protocolVersion,
      vibesysVersion: typeof vibesysVersion === 'string' ? vibesysVersion : null,
    };
  }
  const version = recordVersion(record, path);
  onlyKeys(record, version >= 3 ? [...RECORD_KEYS, ...RECORD_V3_KEYS] : RECORD_KEYS, path);
  const id = instanceId(record['id'], `${path}.id`);
  const status = record['status'];
  if (status !== 'starting' && status !== 'serving') {
    throw new RecordError(`${path}.status must be "starting" or "serving"`);
  }
  const runId =
    version === 1 ? nullableString(record, 'run_id', path) : string(record, 'run_id', path);
  const socketPath = absolutePath(record, 'socket_path', path);
  const vibesysRoot = nullableString(record, 'vibesys_root', path);
  const pid = number(record, 'pid', path);
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new RecordError(`${path}.pid must be a positive integer`);
  }
  return {
    kind: 'compatible',
    instance: {
      id,
      status,
      socketPath,
      projectRoot: string(record, 'project_root', path),
      runId,
      pid,
      startedAt: number(record, 'started_at', path),
      hostname: string(record, 'hostname', path),
      vibesysVersion: string(record, 'vibesys_version', path),
      vibesysRoot,
      ...lifecycle(record, version, path),
    },
  };
}

type RecordVersion = 1 | 2 | 3 | 4;

/**
 * Version 2 names its run from the first write; version 1 (an older host) may not yet. Version 3
 * adds the run's phase, the server's log, and, once it exited, `ended_at`. Version 4 adds the
 * startup stages past `resolving` and their progress.
 */
function recordVersion(record: Record<string, unknown>, path: string): RecordVersion {
  const version = record['version'];
  if (version !== 1 && version !== 2 && version !== 3 && version !== 4) {
    throw new RecordError(`${path}.version must be 1, 2, 3 or 4`);
  }
  return version;
}

function lifecycle(
  record: Record<string, unknown>,
  version: RecordVersion,
  path: string,
): Pick<LiveInstance, 'phase' | 'endedAt'> {
  if (version < 3) return {phase: null, endedAt: null};
  const endedAt = record['ended_at'] == null ? null : number(record, 'ended_at', path);
  return {phase: runPhase(record['phase'], version, `${path}.phase`), endedAt};
}

const RECORD_V3_KEYS = ['phase', 'log_path', 'ended_at'] as const;

const PHASE_KEYS = ['status', 'stage', 'reason'] as const;

function runPhase(value: unknown, version: RecordVersion, path: string): RunPhase {
  const phase = object(value, path);
  onlyKeys(phase, version >= 4 ? [...PHASE_KEYS, 'progress'] : PHASE_KEYS, path);
  const status = phase['status'];
  if (!RUN_STATUSES.some(known => known === status)) {
    throw new RecordError(`${path}.status must be a run status`);
  }
  const reason = phase['reason'] ?? null;
  const progress = phase['progress'] ?? null;
  return {
    status: status as RunPhase['status'],
    stage: nullableString(phase, 'stage', path),
    progress: progress === null ? null : startupProgress(progress, `${path}.progress`),
    reason: reason === null ? null : failureReason(reason, `${path}.reason`),
  };
}

function startupProgress(value: unknown, path: string): StartupProgress {
  const progress = object(value, path);
  onlyKeys(progress, ['done', 'total', 'label'], path);
  const done = number(progress, 'done', path);
  if (!Number.isInteger(done) || done < 0) {
    throw new RecordError(`${path}.done must be a non-negative integer`);
  }
  const total = progress['total'] == null ? null : number(progress, 'total', path);
  if (total !== null && (!Number.isInteger(total) || total < 1)) {
    throw new RecordError(`${path}.total must be a positive integer`);
  }
  return {done, total, label: string(progress, 'label', path)};
}

function failureReason(value: unknown, path: string): NonNullable<RunPhase['reason']> {
  const reason = object(value, path);
  onlyKeys(reason, ['code', 'stage', 'message'], path);
  return {
    code: string(reason, 'code', path),
    stage: string(reason, 'stage', path),
    message: string(reason, 'message', path),
  };
}

/** Parse a `vibesys instances list --json` document. */
export function parseInstanceList(value: unknown): InstanceListing {
  const listing = object(value, 'listing');
  onlyKeys(listing, ['version', 'instances', 'ended', 'unreadable', 'unverified'], 'listing');
  const version = listing['version'];
  if (version !== 1 && version !== 2 && version !== 3 && version !== 4) {
    throw new RecordError('listing.version must be 1, 2, 3 or 4');
  }
  const ended = listing['ended'] ?? [];
  if (!Array.isArray(ended)) throw new RecordError('listing.ended must be a list');
  const instances = listing['instances'];
  if (!Array.isArray(instances)) throw new RecordError('listing.instances must be a list');
  const unverified = listing['unverified'] ?? [];
  if (!Array.isArray(unverified)) throw new RecordError('listing.unverified must be a list of ids');
  return {
    records: instances.map((record, index) =>
      parseInstanceRecord(record, `listing.instances[${index}]`),
    ),
    unverified: unverified.map((id, index) => instanceId(id, `listing.unverified[${index}]`)),
    ...(listing['unreadable'] === undefined
      ? {}
      : {unreadable: unreadableRecords(listing['unreadable'])}),
    ...(listing['ended'] === undefined
      ? {}
      : {
          ended: ended.map((record, index) =>
            parseInstanceRecord(record, `listing.ended[${index}]`),
          ),
        }),
  };
}

function unreadableRecords(value: unknown): NonNullable<InstanceListing['unreadable']> {
  if (!Array.isArray(value)) throw new RecordError('listing.unreadable must be a list');
  return value.map((entry, index) => {
    const path = `listing.unreadable[${index}]`;
    const record = object(entry, path);
    onlyKeys(record, ['id', 'version'], path);
    const version = record['version'] ?? null;
    if (version !== null && !Number.isInteger(version)) {
      throw new RecordError(`${path}.version must be an integer or null`);
    }
    return {id: string(record, 'id', path), version: version as number | null};
  });
}

/** Why `vibesys --detach` started nothing (`DetachedLaunchFailure`). */
export interface DetachedLaunchFailure {
  /** A `DetachedLaunchErrorCode` or a configuration diagnostic code (`invalid_arguments`, ...). */
  readonly code: string;
  readonly stage: string;
  readonly message: string;
  readonly exitCode: number;
  /** The detached server's log, when one was started. */
  readonly logPath: string | null;
  /** For `run_already_live`: the server already driving the run, to attach to instead. */
  readonly liveInstance: InstanceRecord | null;
}

/** What `vibesys --detach` printed: the started server's record, or why it started nothing. */
export type DetachedLaunch =
  | {readonly kind: 'started'; readonly record: InstanceRecord}
  | {readonly kind: 'failed'; readonly failure: DetachedLaunchFailure};

const FAILURE_KEYS = [
  'version',
  'outcome',
  'code',
  'stage',
  'message',
  'exit_code',
  'log_path',
  'live_instance',
] as const;

/** Parse `vibesys --detach`'s one line: a failure has `outcome`, which a record never has. */
export function parseDetachedLaunch(value: unknown): DetachedLaunch {
  const document = object(value, 'launch');
  if (!('outcome' in document)) return {kind: 'started', record: parseInstanceRecord(document)};
  const path = 'launch';
  onlyKeys(document, FAILURE_KEYS, path);
  if (document['version'] !== 1) throw new RecordError(`${path}.version must be 1`);
  if (document['outcome'] !== 'failed') throw new RecordError(`${path}.outcome must be "failed"`);
  const exitCode = number(document, 'exit_code', path);
  const logPath = document['log_path'] ?? null;
  if (logPath !== null && typeof logPath !== 'string') {
    throw new RecordError(`${path}.log_path must be a string or null`);
  }
  const live = document['live_instance'] ?? null;
  return {
    kind: 'failed',
    failure: {
      code: string(document, 'code', path),
      stage: string(document, 'stage', path),
      message: string(document, 'message', path),
      exitCode,
      logPath,
      liveInstance: live === null ? null : parseInstanceRecord(live, `${path}.live_instance`),
    },
  };
}

/** What a user reads about a launch that started nothing. */
export function detachedFailureMessage(failure: DetachedLaunchFailure): string {
  const log = failure.logPath === null ? '' : ` Its log is ${failure.logPath}.`;
  return `${failure.message} (${failure.code} at ${failure.stage}).${log}`;
}

/** What `vibesys instances stop ID --json` observed (`StopOutcome`). */
export type StopOutcome = 'stopped' | 'stopping' | 'not_running' | 'still_running' | 'unsupported';

export interface StopResult {
  readonly id: string;
  readonly outcome: StopOutcome;
  readonly route: 'control_socket' | 'signal' | null;
}

const STOP_OUTCOMES: readonly StopOutcome[] = [
  'stopped',
  'stopping',
  'not_running',
  'still_running',
  'unsupported',
];

/** Parse an `InstanceStopResult`. */
export function parseStopResult(value: unknown): StopResult {
  const path = 'stop';
  const document = object(value, path);
  onlyKeys(document, ['version', 'id', 'outcome', 'route'], path);
  if (document['version'] !== 1) throw new RecordError(`${path}.version must be 1`);
  const outcome = STOP_OUTCOMES.find(known => known === document['outcome']);
  if (outcome === undefined) {
    throw new RecordError(`${path}.outcome must be one of ${STOP_OUTCOMES.join(', ')}`);
  }
  const route = document['route'] ?? null;
  if (route !== null && route !== 'control_socket' && route !== 'signal') {
    throw new RecordError(`${path}.route must be "control_socket", "signal", or null`);
  }
  return {id: string(document, 'id', path), outcome, route};
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

/** `record[key]` when it is a string, null when absent or null; otherwise a `RecordError`. */
function nullableString(record: Record<string, unknown>, key: string, path: string): string | null {
  const value = record[key] ?? null;
  if (value !== null && typeof value !== 'string') {
    throw new RecordError(`${path}.${key} must be a string or null`);
  }
  return value;
}

function string(record: Record<string, unknown>, key: string, path: string): string {
  const value = record[key];
  if (typeof value !== 'string') throw new RecordError(`${path}.${key} must be a string`);
  return value;
}

function instanceId(value: unknown, path: string): string {
  if (!isInstanceId(value)) {
    throw new RecordError(`${path} must be 12 lowercase hex digits`);
  }
  return value;
}

function absolutePath(record: Record<string, unknown>, key: string, path: string): string {
  const value = string(record, key, path);
  if (!value.startsWith('/')) throw new RecordError(`${path}.${key} must be an absolute path`);
  return value;
}

function number(record: Record<string, unknown>, key: string, path: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new RecordError(`${path}.${key} must be a number`);
  }
  return value;
}
