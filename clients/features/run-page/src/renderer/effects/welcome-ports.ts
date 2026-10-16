/**
 * The Run screen's ports over the welcome bridge, as the desktop app's shell window provides them:
 * stops (already confirmed on the screen) and resumes go through the main process's run control,
 * Archive through the settings store, and the logs through the host's logs channel that the main
 * process holds for this window until `releaseLogs`. Each command names the run's current
 * server instance, read from its record when the command is sent.
 */
import {settled, type WelcomeBridge, type WelcomeResult} from '@vibesys/ipc';
import type {LogListOutcome, LogReadInput, LogReadOutcome, LogTransport} from '@vibesys/logs';
import {exactResumeRunId, type RunRef} from '@vibesys/runs';
import type {AbortSignalLike} from '@vibesys/transport';
import type {RunPagePorts} from './store.js';

/** The welcome bridge's calls the Run screen makes. */
export type RunPageBridge = Pick<
  WelcomeBridge,
  'stopConfirmed' | 'resume' | 'archive' | 'logs' | 'releaseLogs'
>;

/** What the screen's run is, beyond its identity, at the moment a command is sent. */
export interface RunPageTarget {
  readonly run: RunRef;
  readonly project: string;
  /** The server instance driving or last driving the run; null before the record is known. */
  instance(): string | null;
}

/** The app's own navigation and windows, which the bridge does not carry. */
export interface RunPageNavigation {
  runAgain(): void;
  openWindow(): void;
  pause(): void;
  resume(): void;
}

type LogsAnswer = LogListOutcome | LogReadOutcome;

function isAnswer(value: unknown): value is LogsAnswer {
  return typeof value === 'object' && value !== null && 'ok' in value;
}

function failed(message: string): LogsAnswer {
  return {ok: false, failure: {code: 'transport', message}};
}

function answer(result: WelcomeResult<LogsAnswer | null>): LogsAnswer {
  if (!result.ok) return failed(result.error);
  return isAnswer(result.value) ? result.value : failed('the app sent no log answer');
}

let readTokens = 0;

/** A read the screen can withdraw: aborting it withdraws the helper's long poll too. */
function readOver(bridge: RunPageBridge, target: RunPageTarget): LogTransport {
  return {
    read: (input: LogReadInput, signal: AbortSignalLike) => {
      readTokens += 1;
      const token = `run-page-${readTokens}`;
      const {host} = target.run;
      return new Promise<LogReadOutcome>((resolve, reject) => {
        const aborted = (): void => {
          reject(signal.reason);
          void settled(bridge.logs(host, input.instance_id, {kind: 'withdraw', token}));
        };
        if (signal.aborted) {
          aborted();
          return;
        }
        signal.addEventListener('abort', aborted, {once: true});
        void settled(bridge.logs(host, input.instance_id, {kind: 'read', token, read: input})).then(
          result => {
            signal.removeEventListener('abort', aborted);
            resolve(answer(result) as LogReadOutcome);
          },
        );
      });
    },
  };
}

export function welcomeRunPorts(
  bridge: RunPageBridge,
  target: RunPageTarget,
  navigation: RunPageNavigation,
): RunPagePorts & {release(): void} {
  const {host} = target.run;
  return {
    stop: async force => {
      const instance = target.instance();
      if (instance === null) return false;
      return (await settled(bridge.stopConfirmed(host, instance, force))).ok;
    },
    pause: () => navigation.pause(),
    resume: () => navigation.resume(),
    resumeEnded: () =>
      void settled(bridge.resume(host, target.project, exactResumeRunId(target.run.runId))),
    openWindow: () => navigation.openWindow(),
    archive: () => void settled(bridge.archive(target.run, true)),
    runAgain: () => navigation.runAgain(),
    logs: {
      list: async instanceId =>
        answer(await settled(bridge.logs(host, instanceId, {kind: 'list'}))) as LogListOutcome,
      transport: readOver(bridge, target),
    },
    release: () => void settled(bridge.releaseLogs()),
  };
}
