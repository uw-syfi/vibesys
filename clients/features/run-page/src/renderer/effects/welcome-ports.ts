/**
 * The Run screen's ports over the welcome bridge, as the desktop app's shell window provides them:
 * stops (already confirmed on the screen) and resumes go through the main process's run control,
 * Archive through the settings store, and the logs through the host's logs channel that the main
 * process holds for this window until `releaseLogs`. Each command names the run's current
 * server instance, read from its record when the command is sent. A live run is attached to the
 * window (`attach`, the same run control a run page uses) and its session runs over the window's
 * relay (`run-connection.ts`); Pause and Resume are commands on that session.
 */
import {type RunStatus, validateRunEvent} from '@vibesys/contracts';
import {recordedProjectRoot} from '@vibesys/hosts';
import {
  type DesktopBridge,
  type RunHistoryPage,
  type RunSpanPage,
  type RunSummaryResult,
  settled,
  type WelcomeBridge,
  type WelcomeResult,
} from '@vibesys/ipc';
import type {LogListOutcome, LogReadInput, LogReadOutcome, LogTransport} from '@vibesys/logs';
import {WebSession, type WebSessionOptions} from '@vibesys/run-session';
import {exactResumeRunId, type RunRef} from '@vibesys/runs';
import type {AbortSignalLike} from '@vibesys/transport';
import {
  type HistoryReply,
  type SpanDetailReply,
  type SummaryReply,
  summaryFacts,
} from '../../state/index.js';
import type {LiveSession, SessionObserver} from './live-session.js';
import {type PortMessages, welcomeRunBridge} from './run-connection.js';
import type {RunPagePorts} from './store.js';
import {sharedTracePages, type TracePageCache, traceReader} from './trace-pages.js';
import type {RunPageAddress} from './use-zoom.js';

/** The welcome bridge's calls the Run screen makes. */
export type RunPageBridge = Pick<
  WelcomeBridge,
  | 'resume'
  | 'archive'
  | 'logs'
  | 'releaseLogs'
  | 'attach'
  | 'attachReader'
  | 'releaseRun'
  | 'connectRun'
  | 'onWake'
  | 'readEvents'
  | 'readTrace'
  | 'readSummary'
  | 'readSpan'
  | 'harnesses'
>;

/** The page the session runs in: where the preload's ports arrive, and the app's session wiring. */
export interface RunPagePage {
  readonly messages: PortMessages;
  /** The window's address and keys, where the screen keeps its zoom. */
  readonly address: RunPageAddress;
  /** The window's clock, in epoch milliseconds. */
  now(): number;
  /** The session's transport and lifecycle over the run's bridge (the app builds transports). */
  session(bridge: DesktopBridge): Omit<WebSessionOptions, 'onTracePublication'>;
}

/** What the screen's run is, beyond its identity, at the moment a command is sent. */
export interface RunPageTarget {
  readonly run: RunRef;
  /** The recorded project root at the moment of the call, or '' when the run has none. */
  project(): string;
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
    // The page's size as decoded: the loading screen's "read so far" (the wire carries no count).
    const bytes = JSON.stringify(page.events).length;
    return {...page, bytes, events: page.events.map(validateRunEvent)};
  } catch (error) {
    return {
      kind: 'failed',
      message: `the run's history holds an unreadable event: ${String(error)}`,
    };
  }
}

function spanReply(page: RunSpanPage): SpanDetailReply {
  if (page.kind === 'unsupported') return page;
  try {
    return {...page, events: page.events.map(validateRunEvent)};
  } catch (error) {
    return {kind: 'failed', message: `the span holds an unreadable event: ${String(error)}`};
  }
}

/**
 * A summary read as the screen takes it: validated into its snapshot and seed events. A summary
 * that is not a valid one fails the read, as a history with an unreadable event does.
 */
function summaryReply(
  result: WelcomeResult<RunSummaryResult>,
  runId: string,
  status: RunStatus,
): SummaryReply {
  if (!result.ok) return {kind: 'failed', message: result.error};
  if (result.value.kind !== 'summary') return result.value;
  try {
    return {kind: 'summary', ...summaryFacts(result.value.summary, runId, status)};
  } catch (error) {
    return {kind: 'failed', message: `the run’s summary is not readable: ${String(error)}`};
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
    onTracePublication: publication => observer.data(publication),
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
  const failure = (error: unknown): {readonly ok: false; readonly message: string} => ({
    ok: false,
    message: error instanceof Error ? error.message : String(error),
  });
  return {
    chat: {
      ask: async (input, signal) => {
        try {
          await session.ask(input.text, {threadId: input.threadId, signal});
          return {ok: true};
        } catch (error) {
          return failure(error);
        }
      },
      createThread: async (harness, signal) => {
        try {
          const thread = await session.createChatThread(harness, {signal});
          return {ok: true, threadId: thread.thread_id};
        } catch (error) {
          return failure(error);
        }
      },
      control: async request => {
        const outcome = await session.command({type: 'command.control', control: request});
        return outcome.kind === 'acknowledged'
          ? {kind: 'acknowledged'}
          : {kind: 'rejected', message: outcome.message, code: outcome.code ?? null};
      },
    },
    command: async command => {
      const outcome = await session.command({
        type: 'command.control',
        control: {
          control_id: crypto.randomUUID(),
          action: command.kind === 'pause-now' ? 'pause_now' : 'pause',
        },
      });
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
  traces: TracePageCache = sharedTracePages,
): RunPagePorts & {releaseLogs(): void} {
  const {host} = target.run;
  return {
    attach: async instance => {
      const result = await settled(bridge.attach(host, instance));
      return result.ok ? null : result.error;
    },
    release: () => void settled(bridge.releaseRun()),
    attachReader: async () => {
      const result = await settled(bridge.attachReader(host, target.project(), target.run.runId));
      return result.ok ? result.value : {kind: 'failed', message: result.error};
    },
    openSession: observer => openWebSession(bridge, page, observer),
    readHistory: async (from, max) => {
      const result = await settled(
        bridge.readEvents(host, target.project(), target.run.runId, from, max),
      );
      return result.ok ? historyReply(result.value) : {kind: 'failed', message: result.error};
    },
    readTrace: traceReader(
      () => [host, target.project(), target.run.runId, target.instance() ?? ''].join('\0'),
      traces,
      (from, max) => settled(bridge.readTrace(host, target.project(), target.run.runId, from, max)),
    ),
    readSummary: async status => {
      const result = await settled(bridge.readSummary(host, target.project(), target.run.runId));
      return summaryReply(result, target.run.runId, status);
    },
    readSpan: async (spanId, from, max) => {
      const result = await settled(
        bridge.readSpan(host, target.project(), target.run.runId, spanId, from, max),
      );
      return result.ok ? spanReply(result.value) : {kind: 'failed', message: result.error};
    },
    resumeEnded: () => {
      const project = recordedProjectRoot(target.project());
      if (project === undefined) return;
      void settled(bridge.resume(host, project, exactResumeRunId(target.run.runId)));
    },
    openWindow: () => navigation.openWindow(),
    now: () => page.now(),
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
