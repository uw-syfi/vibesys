/**
 * What the supervisor's check and stream reports mean for one attached run on one `Host`.
 *
 * The check restores the host link (`ensureLink`, which may prompt only when the user asked for a
 * retry) and reads the host's registry: the run must still be listed and speak this client's
 * protocol. Host errors become the supervisor's closed set of failures here, and nowhere else.
 */
import type {Duplex} from 'node:stream';
import type {Endpoint, Host} from './host.js';
import {HostError} from './host.js';
import {parseInstanceList, RecordError, versionSkewMessage} from './instances.js';
import type {CheckFailure, CheckOutcome, StreamEnd} from './supervisor.js';

/** The run a window is attached to, and how to name its host in messages. */
export interface AttachedRun {
  readonly host: Host;
  readonly hostName: string;
  /** The host's vibesys command, named in a version mismatch message. */
  readonly vibesysCommand: string;
  /** The registry id, or null for a bare socket (`--socket`), which has no registry entry. */
  readonly instanceId: string | null;
  readonly endpoint: Endpoint;
}

/** Restore the link to `run`'s host and confirm the run is there and readable. */
export async function checkAttachment(
  run: AttachedRun,
  interactive: boolean,
): Promise<CheckOutcome> {
  try {
    if (interactive) await run.host.ensureLink();
    if (run.instanceId === null) {
      // No registry entry to read: a dial is the only evidence the server is there.
      (await run.host.dial(run.endpoint)).destroy();
      return {ok: true};
    }
    const listing = parseInstanceList(await run.host.invoke(['instances', 'list', '--json']));
    const record = listing.records.find(candidate =>
      candidate.kind === 'compatible'
        ? candidate.instance.id === run.instanceId
        : candidate.id === run.instanceId,
    );
    if (record === undefined) {
      return listing.unverified.includes(run.instanceId)
        ? {ok: false, cause: 'link', detail: `the host could not confirm run ${run.instanceId}`}
        : {ok: false, cause: 'run-gone', detail: `run ${run.instanceId} is no longer running`};
    }
    if (record.kind === 'incompatible') {
      return {
        ok: false,
        cause: 'version-skew',
        detail: versionSkewMessage({
          hostName: run.hostName,
          vibesysCommand: run.vibesysCommand,
          protocolVersion: record.protocolVersion,
          vibesysVersion: record.vibesysVersion,
        }),
      };
    }
    return {ok: true};
  } catch (error) {
    return {ok: false, cause: failureOf(error), detail: (error as Error).message};
  }
}

function failureOf(error: unknown): CheckFailure {
  if (error instanceof RecordError) return 'failed';
  if (!(error instanceof HostError)) return 'failed';
  switch (error.kind) {
    case 'link':
      return 'link';
    case 'auth':
      return 'auth';
    case 'unreachable':
      return 'run-gone';
    case 'failed':
    case 'malformed':
    case 'closed':
      return 'failed';
  }
}

/** How a dial attempt or an open stream ended, for the supervisor. */
function streamEndOf(error: unknown): StreamEnd {
  if (!(error instanceof HostError)) return 'normal';
  switch (error.kind) {
    case 'link':
      return 'link';
    case 'unreachable':
      return 'run-gone';
    case 'auth':
    case 'failed':
    case 'malformed':
      return 'failed';
    case 'closed':
      return 'normal';
  }
}

/** `run`'s dial, reporting every failed dial and abnormal stream end to `report`. */
export function observedDial(
  run: AttachedRun,
  report: (end: StreamEnd) => void,
): () => Promise<Duplex> {
  return async () => {
    let stream: Duplex;
    try {
      stream = await run.host.dial(run.endpoint);
    } catch (error) {
      report(streamEndOf(error));
      throw error;
    }
    stream.once('error', error => report(streamEndOf(error)));
    return stream;
  };
}
