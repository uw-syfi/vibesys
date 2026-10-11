/**
 * The welcome view's requests, parsed at the boundary.
 *
 * The welcome view is a trusted app page, but its requests are still input: `parseWelcomeRequest`
 * turns a channel and its raw arguments into one typed request, validating every value (host keys,
 * paths, ids, task names, run arguments) before anything reaches a host, or throws naming what is
 * wrong. `bindWelcomeIpc` answers the channels only for the welcome view's own web contents.
 */
import {shellWords} from '@vibesys/feature-start-run/state';
import {type HostId, hostFromKey, validHostPath} from '@vibesys/hosts';
import {WELCOME_CHANNELS, type WelcomeResult} from '@vibesys/ipc';
import {exactResumeRunId, isInstanceId, isTaskName, type ResumeRunId} from '@vibesys/runs';
import {ConnectionError} from '../platform/connection/index.js';

/** The narrow IPC boundary needed by the welcome request binder. */
export interface WelcomeIpc {
  handle(
    channel: string,
    listener: (event: WelcomeInvokeEvent, ...args: unknown[]) => Promise<WelcomeResult<unknown>>,
  ): void;
}

export interface WelcomeInvokeEvent {
  readonly senderId: number;
  readonly senderUrl: string | null;
  /** The main-process identity of the exact renderer document that invoked this request. */
  readonly rendererGeneration: string | null;
}

/** One renderer web contents, with document replacement reported by the Electron wiring. */
export interface WelcomeDocument {
  readonly id: number;
  onNewDocument(listener: (retiringGeneration: string | null) => void): void;
  onDestroyed(listener: () => void): void;
}

export type WelcomeRequest =
  | {readonly kind: 'overview'}
  | {readonly kind: 'hosts'}
  | {readonly kind: 'host'; readonly host: HostId}
  | {
      readonly kind: 'set-checkout';
      readonly host: HostId;
      readonly path: unknown;
      readonly change: number;
    }
  | {readonly kind: 'cancel-checkout'; readonly change: number}
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

function operationToken(value: unknown, operation: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`a ${operation} is named by a positive integer`);
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
    start: operationToken(token, 'start'),
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
      return {
        kind: 'set-checkout',
        host: hostFromKey(first),
        path: second,
        change: operationToken(third, 'checkout change'),
      };
    case WELCOME_CHANNELS.cancelCheckout:
      return {kind: 'cancel-checkout', change: operationToken(first, 'checkout change')};
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
      return {kind: 'cancel-start', start: operationToken(first, 'start')};
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
 * `url`) only; any other sender is refused. Each request carries the invoking Electron frame's
 * process/routing identity. A replacement document cannot issue work until `retire` has settled
 * every operation belonging to the prior identities.
 */
export function bindWelcomeIpc(
  ipc: WelcomeIpc,
  welcome: WelcomeDocument,
  url: string,
  answer: (request: WelcomeRequest, rendererGeneration: string) => Promise<unknown>,
  retire: (rendererGeneration: string) => Promise<void>,
): void {
  let alive = true;
  /** Every replacement advances this even when Electron cannot identify the retiring frame. */
  let documentEpoch = 0;
  let activeGenerations = new Set<string>();
  const retiredGenerations = new Set<string>();
  let retirement = Promise.resolve();
  const retireCurrent = (retiringGeneration: string | null = null): void => {
    documentEpoch += 1;
    if (retiringGeneration !== null) activeGenerations.add(retiringGeneration);
    const retiring = [...activeGenerations];
    activeGenerations = new Set();
    for (const generation of retiring) retiredGenerations.add(generation);
    retirement = retirement.then(async () => {
      await Promise.all(retiring.map(generation => retire(generation)));
    });
  };
  welcome.onNewDocument(retireCurrent);
  welcome.onDestroyed(() => {
    alive = false;
    retireCurrent();
  });
  for (const channel of Object.values(WELCOME_CHANNELS)) {
    ipc.handle(channel, async (event, ...args) => {
      if (event.senderId !== welcome.id || event.senderUrl?.startsWith(url) !== true) {
        return failure(new Error('not the welcome view'));
      }
      const requestGeneration = event.rendererGeneration;
      const requestEpoch = documentEpoch;
      try {
        if (requestGeneration === null) throw new Error('welcome view document is unavailable');
        await retirement;
        if (!alive || requestEpoch !== documentEpoch || retiredGenerations.has(requestGeneration))
          throw new Error('welcome view was replaced');
        activeGenerations.add(requestGeneration);
        return {
          ok: true,
          value: await answer(parseWelcomeRequest(channel, args), requestGeneration),
        };
      } catch (error) {
        return failure(error);
      }
    });
  }
}
