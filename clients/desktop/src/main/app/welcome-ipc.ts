/**
 * The welcome view's requests, parsed at the boundary.
 *
 * The welcome view is a trusted app page, but its requests are still input: `parseWelcomeRequest`
 * turns a channel and its raw arguments into one typed request, validating every value (host keys,
 * paths, ids, task names, run arguments) before anything reaches a host, or throws naming what is
 * wrong. `bindWelcomeIpc` answers the channels only for the welcome view's own web contents.
 */
import type {IpcMain, IpcMainInvokeEvent} from 'electron';
import {type HostId, hostFromKey, validHostPath} from '../../common/hosts.js';
import {isInstanceId, isTaskName} from '../../common/ids.js';
import {exactResumeRunId, type ResumeRunId} from '../../common/resume-run.js';
import {shellWords} from '../../common/shell-words.js';
import {WELCOME_CHANNELS, type WelcomeResult} from '../../common/welcome-protocol.js';
import {ConnectionError} from '../platform/connection/index.js';

export type WelcomeRequest =
  | {readonly kind: 'overview'}
  | {readonly kind: 'hosts'}
  | {readonly kind: 'host'; readonly host: HostId}
  | {readonly kind: 'set-checkout'; readonly host: HostId; readonly path: unknown}
  | {readonly kind: 'sign-in'; readonly host: HostId}
  | {readonly kind: 'tasks'; readonly host: HostId; readonly project: string}
  /** List `directory` (a path ending in `/`) for completion. */
  | {readonly kind: 'complete'; readonly host: HostId; readonly directory: string}
  | {readonly kind: 'attach'; readonly host: HostId; readonly instance: string}
  | {
      readonly kind: 'start';
      readonly host: HostId;
      readonly project: string;
      readonly task: string;
      readonly args: readonly string[];
      readonly start: number;
    }
  | {readonly kind: 'cancel-start'; readonly start: number}
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
    }
  | {readonly kind: 'show-run'}
  | {readonly kind: 'show-welcome'}
  | {readonly kind: 'retry'};

type Channel = (typeof WELCOME_CHANNELS)[keyof typeof WELCOME_CHANNELS];

function instance(value: unknown, verb: string): string {
  if (!isInstanceId(value)) throw new Error(`pick a run to ${verb}`);
  return value;
}

function project(value: unknown): string {
  return validHostPath(value, 'the project directory');
}

/** A directory to list: a host path, as the listing's prefix (ending in `/`). */
function directory(value: unknown): string {
  const path = validHostPath(value, 'the directory to complete');
  return path.endsWith('/') ? path : `${path}/`;
}

function startToken(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error('a start is named by a positive integer');
  }
  return value;
}

function start(args: readonly unknown[]): WelcomeRequest {
  const [key, path, task, words, token] = args;
  if (!isTaskName(task)) throw new Error("pick one of the project's tasks");
  return {
    kind: 'start',
    host: hostFromKey(key),
    project: project(path),
    task,
    args: typeof words === 'string' ? shellWords(words) : [],
    start: startToken(token),
  };
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseWelcomeRequest(channel: Channel, args: readonly unknown[]): WelcomeRequest {
  const [first, second, third] = args;
  switch (channel) {
    case WELCOME_CHANNELS.overview:
      return {kind: 'overview'};
    case WELCOME_CHANNELS.hosts:
      return {kind: 'hosts'};
    case WELCOME_CHANNELS.host:
      return {kind: 'host', host: hostFromKey(first)};
    case WELCOME_CHANNELS.setCheckout:
      return {kind: 'set-checkout', host: hostFromKey(first), path: second};
    case WELCOME_CHANNELS.signIn:
      return {kind: 'sign-in', host: hostFromKey(first)};
    case WELCOME_CHANNELS.tasks:
      return {kind: 'tasks', host: hostFromKey(first), project: project(second)};
    case WELCOME_CHANNELS.complete:
      return {kind: 'complete', host: hostFromKey(first), directory: directory(second)};
    case WELCOME_CHANNELS.attach:
      return {kind: 'attach', host: hostFromKey(first), instance: instance(second, 'attach to')};
    case WELCOME_CHANNELS.start:
      return start(args);
    case WELCOME_CHANNELS.cancelStart:
      return {kind: 'cancel-start', start: startToken(first)};
    case WELCOME_CHANNELS.resume:
      return {
        kind: 'resume',
        host: hostFromKey(first),
        project: project(second),
        run: exactResumeRunId(third),
      };
    case WELCOME_CHANNELS.stop:
      return {
        kind: 'stop',
        host: hostFromKey(first),
        instance: instance(second, 'stop'),
        force: third === true,
      };
    case WELCOME_CHANNELS.showRun:
      return {kind: 'show-run'};
    case WELCOME_CHANNELS.showWelcome:
      return {kind: 'show-welcome'};
    case WELCOME_CHANNELS.retry:
      return {kind: 'retry'};
  }
}

function failure(error: unknown): WelcomeResult<never> {
  return {
    ok: false,
    error: error instanceof Error ? error.message : String(error),
    authNeeded: error instanceof ConnectionError && error.reason === 'auth',
  };
}

/**
 * Answer every welcome channel with `answer`, for `welcome` (the welcome view's web contents on
 * `url`) only; any other sender is refused.
 */
export function bindWelcomeIpc(
  ipc: IpcMain,
  welcome: {readonly id: number},
  url: string,
  answer: (request: WelcomeRequest) => Promise<unknown>,
): void {
  for (const channel of Object.values(WELCOME_CHANNELS)) {
    ipc.handle(channel, async (event: IpcMainInvokeEvent, ...args: unknown[]) => {
      if (event.sender.id !== welcome.id || event.senderFrame?.url.startsWith(url) !== true) {
        return failure(new Error('not the welcome view'));
      }
      try {
        return {ok: true, value: await answer(parseWelcomeRequest(channel, args))};
      } catch (error) {
        return failure(error);
      }
    });
  }
}
