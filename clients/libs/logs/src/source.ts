/**
 * A run's logs as one page sees them: the streams its run recorded (`logs.list`) and each
 * stream's bytes (`logs.read`, the `LogTransport` a follower reads through). The desktop provides
 * one per run page over the host's logs channel (`docs/contributing/desktop-connection.md`,
 * "Logs"); the page never names a host or a channel.
 */
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {AbortSignalLike} from '@vibesys/transport';
import type {LogReadFailure} from './follow.js';
import type {LogTransport} from './follower.js';

/**
 * What a listing settles to. A listing names the instance the page is bound to, from the record
 * the app attached to, so the page's reads name it too; it never derives one.
 */
export type LogListOutcome =
  | {
      readonly ok: true;
      readonly instanceId: string;
      /** Oldest first, as the run's store indexes them. */
      readonly streams: readonly LogStream[];
    }
  | {readonly ok: false; readonly failure: LogReadFailure};

export interface RunLogSource extends LogTransport {
  /** The run's streams. Resolves expected failures as `{ok: false}`; honors `signal` like `read`. */
  list(signal: AbortSignalLike): Promise<LogListOutcome>;
}

/** Whether `failure` says the host offers no logs at all, which a view shows as unsupported. */
export function logsUnsupported(failure: LogReadFailure): boolean {
  return failure.code === 'feature_not_negotiated';
}
