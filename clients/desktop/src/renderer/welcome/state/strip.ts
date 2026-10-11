/**
 * The title strip's selectors: how the attached run's connection reads, and which controls the
 * window state allows. Retry shows only on a link a retry can fix, never on an ended run.
 */

import type {ConnectionStatus} from '../../../common/connection-status.js';
import {type ResumeDecision, resumeDecision} from '../../../common/resume-run.js';
import {stopKey} from '../../../common/stop-run.js';
import type {ChromeState} from '../../../common/welcome-protocol.js';

/** The attached run's link as the strip shows it. */
export type RunLink = 'connecting' | 'connected' | 'reconnecting' | 'stuck' | 'ended';

export function runLink(connection: ConnectionStatus): RunLink {
  switch (connection.kind) {
    case 'connecting':
    case 'connected':
      return connection.kind;
    case 'reconnecting':
    case 'offline':
      return 'reconnecting';
    case 'run-ended':
      return 'ended';
    case 'auth-needed':
    case 'incompatible':
    case 'closed':
      return 'stuck';
  }
}

function connectionLabel(connection: ConnectionStatus): string {
  switch (connection.kind) {
    case 'reconnecting':
      return `reconnecting (attempt ${connection.attempt})`;
    case 'offline':
      return 'offline, waiting for the network';
    case 'auth-needed':
      return 'sign-in needed';
    case 'run-ended':
      return 'run ended';
    case 'incompatible':
      return 'version mismatch';
    case 'closed':
      return 'closed';
    default:
      return connection.kind;
  }
}

function connectionDetail(connection: ConnectionStatus, host: string): string {
  switch (connection.kind) {
    case 'auth-needed':
      return `${host} asked for credentials that were not given (${connection.detail}). Retry to sign in again.`;
    case 'run-ended':
      return `The run on ${host} is no longer running. Its transcript stays on screen; click the host to attach to another run.`;
    case 'offline':
    case 'incompatible':
      return connection.detail;
    default:
      return '';
  }
}

/** One line for the strip: the connection's state, and why when there is something to say. */
export function connectionText(attached: NonNullable<ChromeState['attached']>): string {
  const label = connectionLabel(attached.connection);
  const detail = connectionDetail(attached.connection, attached.hostLabel);
  return detail === '' ? label : `${label}: ${detail}`;
}

/** Which title-strip controls show for the window's state, and the stop line beside them. */
export interface StripControls {
  readonly stop: boolean;
  readonly force: boolean;
  /** The exact resume, why it is unavailable, or null while the run has not ended. */
  readonly resume: ResumeDecision | null;
  /** Retry the connection: only while it is stuck on something a retry can fix. */
  readonly retry: boolean;
  /** "Back to run": the welcome view fills the window while a run is attached. */
  readonly back: boolean;
  readonly stopText: string;
  readonly stopError: boolean;
}

const NO_STRIP: StripControls = {
  stop: false,
  force: false,
  resume: null,
  retry: false,
  back: false,
  stopText: '',
  stopError: false,
};

export function stripControls(state: ChromeState): StripControls {
  const attached = state.attached;
  if (attached === null) return NO_STRIP;
  const stop = state.stops[stopKey(attached.host, attached.instanceId)];
  const ended = runLink(attached.connection) === 'ended' || stop?.phase === 'ended';
  return {
    stop: !ended && stop?.phase !== 'stopping',
    force: !ended && (stop?.canForce ?? false),
    resume: ended ? resumeDecision(attached.runId) : null,
    retry: runLink(attached.connection) === 'stuck' && !ended,
    back: state.mode === 'welcome',
    stopText: ended ? '' : (stop?.text ?? ''),
    stopError: !ended && stop?.phase === 'error',
  };
}
