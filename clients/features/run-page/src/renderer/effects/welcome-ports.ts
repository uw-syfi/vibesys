/**
 * The Run screen's ports over the welcome bridge, as the desktop app's shell window provides them:
 * stops (already confirmed on the screen) and resumes go through the main process's run control,
 * Archive through the settings store, and the logs through the host's logs channel that the main
 * process holds for this window until `releaseLogs`. Each command names the run's current
 * server instance, read from its record when the command is sent. A live run is attached to the
 * window (`attach`, the same run control a run page uses) and its session runs over the window's
 * relay (`run-connection.ts`); Pause and Resume are commands on that session.
 */
import {validateRunEvent} from '@vibesys/contracts';
import {
  type DesktopBridge,
  type RunHistoryPage,
  settled,
  type WelcomeBridge,
  type WelcomeResult,
} from '@vibesys/ipc';
import type {LogListOutcome, LogReadInput, LogReadOutcome, LogTransport} from '@vibesys/logs';
import {type RunAction, WebSession, type WebSessionOptions} from '@vibesys/run-session';
import {exactResumeRunId, type RunRef} from '@vibesys/runs';
import type {AbortSignalLike} from '@vibesys/transport';
import type {HistoryReply, RunData} from '../../state/index.js';
import type {LiveSession, SessionObserver} from './live-session.js';
import {type PortMessages, welcomeRunBridge} from './run-connection.js';
import type {RunPagePorts} from './store.js';

/** The welcome bridge's calls the Run screen makes. */
export type RunPageBridge = Pick<
  WelcomeBridge,
  | 'stopConfirmed'
  | 'resume'
  | 'archive'
  | 'logs'
  | 'releaseLogs'
  | 'attach'
  | 'releaseRun'
  | 'connectRun'
  | 'onWake'
  | 'readEvents'
>;

/** The page the session runs in: where the preload's ports arrive, and the app's session wiring. */
export interface RunPagePage {
  readonly messages: PortMessages;
  /** The session's transport and lifecycle over the run's bridge (the app builds transports). */
  session(bridge: DesktopBridge): Omit<WebSessionOptions, 'onRunAction'>;
}

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
}

/** A history page with each recorded event validated; an event that is not one fails the page. */
function historyReply(page: RunHistoryPage): HistoryReply {
  if (page.kind === 'unsupported') return page;
  try {
    return {...page, events: page.events.map(validateRunEvent)};
  } catch (error) {
    return {
      kind: 'failed',
      message: `the run's history holds an unreadable event: ${String(error)}`,
    };
  }
}

/** What one fold action of the session adds to the screen's trace slice. */
function runData(action: RunAction): RunData {
  switch (action.type) {
    case 'snapshot':
      return {kind: 'snapshot', snapshot: action.snapshot};
    case 'events':
    case 'response-events':
      return {kind: 'events', events: action.events};
    case 'batch':
      return action.reconciliation.kind === 'rebootstrap'
        ? {kind: 'rebootstrap', events: action.message.events}
        : {kind: 'events', events: action.message.events};
  }
}

/** A `WebSession` over the window's relay, reporting to `observer`. */
function openWebSession(
  bridge: RunPageBridge,
  page: RunPagePage,
  observer: SessionObserver,
): LiveSession {
  const session = new WebSession({
    ...page.session(welcomeRunBridge(bridge, page.messages)),
    onRunAction: action => observer.data(runData(action)),
  });
  const reportSession = (): void => {
    const view = session.getState();
    observer.session({
      hasSnapshot: view.hasSnapshot,
      streaming: view.status === 'connected',
      controls: view.controls.status === 'connected',
      error: view.error?.message ?? null,
    });
  };
  session.subscribe(reportSession);
  reportSession();
  session.store.subscribe(() => observer.core(session.store.getState()));
  void session.start();
  return {
    command: async command => {
      const outcome = await session.command(
        command.kind === 'pause'
          ? {type: 'command.pause'}
          : {type: 'command.resume', when: command.when},
      );
      return outcome.kind === 'acknowledged'
        ? null
        : (outcome.message ?? `the run refused ${command.kind}`);
    },
    close: () => void session.close(),
  };
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
  page: RunPagePage,
): RunPagePorts & {releaseLogs(): void} {
  const {host} = target.run;
  return {
    stop: async force => {
      const instance = target.instance();
      if (instance === null) return false;
      return (await settled(bridge.stopConfirmed(host, instance, force))).ok;
    },
    attach: async instance => {
      const result = await settled(bridge.attach(host, instance));
      return result.ok ? null : result.error;
    },
    release: () => void settled(bridge.releaseRun()),
    openSession: observer => openWebSession(bridge, page, observer),
    readHistory: async (from, max) => {
      const result = await settled(
        bridge.readEvents(host, target.project, target.run.runId, from, max),
      );
      return result.ok ? historyReply(result.value) : {kind: 'failed', message: result.error};
    },
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
    releaseLogs: () => void settled(bridge.releaseLogs()),
  };
}
