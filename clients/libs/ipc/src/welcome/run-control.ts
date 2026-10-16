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
  releaseRun: 'vibesys:welcome:release-run',
  readEvents: 'vibesys:welcome:read-events',
} as const;

/** The most events one history page may ask for. */
export const HISTORY_PAGE_MAX = 1000;

/**
 * One page of a run's recorded events (`runs.read_events`, feature `run_history`): the events from
 * the asked sequence on, as the helper recorded them (the page validates each before folding it),
 * where the next page starts, and whether the history is complete; or that this host's VibeSys
 * does not serve history.
 */
export type RunHistoryPage =
  | {
      readonly kind: 'page';
      readonly events: readonly unknown[];
      readonly next: number;
      readonly complete: boolean;
    }
  | {readonly kind: 'unsupported'};

/**
 * The window message type that hands the shell page one run connection's port: `connectRun(id)`
 * makes the preload post `{type: RUN_PORT_MESSAGE, id}` to the page with the port as its transfer,
 * because a `MessagePort` cannot cross the context bridge as a value.
 */
export const RUN_PORT_MESSAGE = 'vibesys:run-port';

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
  /**
   * Open one connection to the run the window attached for this view (`attach` binds it), over
   * the same relay a run page uses; the port arrives as a `RUN_PORT_MESSAGE` window message named
   * `id`. A view with no attached run gets a port the main process closes at once.
   */
  connectRun(id: string): void;
  /** The machine woke and the attached run's host is reachable again: redial now. */
  onWake(listener: () => void): void;
  /** Let the attached run go: the view shows no live run now. Safe when none is attached. */
  releaseRun(): Promise<WelcomeResult<null>>;
  /** One page of run `runId`'s recorded events in `project` on `host`, from `fromSequence`. */
  readEvents(
    host: HostKey,
    project: string,
    runId: string,
    fromSequence: number,
    max: number,
  ): Promise<WelcomeResult<RunHistoryPage>>;
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
  | {readonly kind: 'retry'}
  | {readonly kind: 'release-run'}
  | {
      readonly kind: 'read-events';
      readonly host: HostId;
      readonly project: string;
      readonly runId: string;
      readonly from: number;
      readonly max: number;
    };

function forceStop(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new Error('force stop must be a boolean');
  return value;
}

function sequence(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new Error('the first sequence to read must be a non-negative integer');
  }
  return value;
}

function pageSize(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) {
    throw new Error('the page size must be a positive integer');
  }
  if (value > HISTORY_PAGE_MAX) throw new Error(`the page size is at most ${HISTORY_PAGE_MAX}`);
  return value;
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseRunControlRequest(
  channel: ChannelOf<typeof RUN_CONTROL_CHANNELS>,
  args: readonly unknown[],
): RunControlRequest {
  const [first, second, third, fourth, fifth] = args;
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
    case RUN_CONTROL_CHANNELS.releaseRun:
      return {kind: 'release-run'};
    case RUN_CONTROL_CHANNELS.readEvents:
      return {
        kind: 'read-events',
        host: hostFromKey(first),
        project: project(second),
        runId: exactResumeRunId(third),
        from: sequence(fourth),
        max: pageSize(fifth),
      };
  }
}
