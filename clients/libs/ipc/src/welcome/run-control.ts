/**
 * The run-control feature's welcome requests: attach, resume, stop (with the app's question, or
 * already confirmed by the view that asked), retry, and switching between
 * the run and the welcome view.
 */
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import {exactResumeRunId, type ResumeRunId} from '@vibesys/runs';
import type {WelcomeResult} from './types.js';
import {type ChannelOf, instance, project} from './values.js';

export const RUN_CONTROL_CHANNELS = {
  attach: 'vibesys:welcome:attach',
  resume: 'vibesys:welcome:resume',
  showRun: 'vibesys:welcome:show-run',
  showWelcome: 'vibesys:welcome:show-welcome',
  retry: 'vibesys:welcome:retry',
  stop: 'vibesys:welcome:stop',
  stopConfirmed: 'vibesys:welcome:stop-confirmed',
} as const;

/** The welcome bridge's requests the run-control feature makes. */
export interface RunControlWelcomeBridge {
  attach(host: HostKey, instance: string): Promise<WelcomeResult<null>>;
  resume(host: HostKey, project: string, run: ResumeRunId): Promise<WelcomeResult<null>>;
  showRun(): Promise<WelcomeResult<null>>;
  showWelcome(): Promise<WelcomeResult<null>>;
  retry(): Promise<WelcomeResult<null>>;
  stop(host: HostKey, instance: string, force: boolean): Promise<WelcomeResult<null>>;
  /** Stop without the app's own question: the view already asked the user, who said yes. */
  stopConfirmed(host: HostKey, instance: string, force: boolean): Promise<WelcomeResult<null>>;
}

export type RunControlRequest =
  | {readonly kind: 'attach'; readonly host: HostId; readonly instance: string}
  | {
      readonly kind: 'resume';
      readonly host: HostId;
      readonly project: string;
      /** The exact run to resume. */
      readonly run: ResumeRunId;
    }
  | {
      readonly kind: 'stop';
      readonly host: HostId;
      readonly instance: string;
      readonly force: boolean;
      /** Whether the view already asked the user, so the app does not ask again. */
      readonly confirmed: boolean;
    }
  | {readonly kind: 'show-run'}
  | {readonly kind: 'show-welcome'}
  | {readonly kind: 'retry'};

function forceStop(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new Error('force stop must be a boolean');
  return value;
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseRunControlRequest(
  channel: ChannelOf<typeof RUN_CONTROL_CHANNELS>,
  args: readonly unknown[],
): RunControlRequest {
  const [first, second, third] = args;
  switch (channel) {
    case RUN_CONTROL_CHANNELS.attach:
      return {kind: 'attach', host: hostFromKey(first), instance: instance(second, 'attach to')};
    case RUN_CONTROL_CHANNELS.resume:
      return {
        kind: 'resume',
        host: hostFromKey(first),
        project: project(second),
        run: exactResumeRunId(third),
      };
    case RUN_CONTROL_CHANNELS.stop:
    case RUN_CONTROL_CHANNELS.stopConfirmed:
      return {
        kind: 'stop',
        host: hostFromKey(first),
        instance: instance(second, 'stop'),
        force: forceStop(third),
        confirmed: channel === RUN_CONTROL_CHANNELS.stopConfirmed,
      };
    case RUN_CONTROL_CHANNELS.showRun:
      return {kind: 'show-run'};
    case RUN_CONTROL_CHANNELS.showWelcome:
      return {kind: 'show-welcome'};
    case RUN_CONTROL_CHANNELS.retry:
      return {kind: 'retry'};
  }
}
