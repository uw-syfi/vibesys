/**
 * The welcome view's requests, answered at the boundary.
 *
 * The welcome view is a trusted app page, but its requests are still input: every request is
 * parsed by `@vibesys/ipc`'s `parseWelcomeRequest`, which validates every value (host keys, paths,
 * ids, task names) before anything reaches a host, or throws naming what is wrong.
 * `bindWelcomeIpc` answers the channels only for the welcome view's own web contents.
 */
import {ConnectionError} from '@vibesys/connection';
import {
  parseWelcomeRequest,
  WELCOME_CHANNELS,
  type WelcomeRequest,
  type WelcomeResult,
} from '@vibesys/ipc';
import {isSameAppPage} from './app-assets.js';

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

/** Stable ownership of the current welcome WebContents, across its one allowed recreation. */
export interface WelcomeDocument {
  /** The exact current welcome WebContents, or null while it is being replaced or closed. */
  currentId(): number | null;
  onNewDocument(listener: (retiringGeneration: string | null) => void): void;
  /** The stable welcome owner closed with its native window. */
  onDestroyed(listener: () => void): void;
}

/** The main-process identity of an exact renderer document, when Electron can provide it. */
export function rendererGeneration(
  frame: {readonly processId: number; readonly routingId: number} | null | undefined,
): string | null {
  return frame === null || frame === undefined ? null : `${frame.processId}:${frame.routingId}`;
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
 * process/routing identity. `revoke` synchronously removes each replaced document's authority;
 * a replacement document cannot issue work until `retire` has settled every asynchronous cleanup
 * belonging to the prior identities. A `retire` implementation owns reporting its cleanup
 * failures; rejection still counts as settlement and cannot skip a sibling generation or poison a
 * later document's barrier.
 */
export function bindWelcomeIpc(
  ipc: WelcomeIpc,
  welcome: WelcomeDocument,
  url: string,
  answer: (request: WelcomeRequest, rendererGeneration: string) => Promise<unknown>,
  revoke: (rendererGeneration: string) => void,
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
    for (const generation of retiring) {
      retiredGenerations.add(generation);
      revoke(generation);
    }
    retirement = retirement.then(async () => {
      await Promise.allSettled(retiring.map(async generation => retire(generation)));
    });
  };
  welcome.onNewDocument(retireCurrent);
  welcome.onDestroyed(() => {
    alive = false;
    retireCurrent();
  });
  for (const channel of Object.values(WELCOME_CHANNELS)) {
    ipc.handle(channel, async (event, ...args) => {
      if (event.senderId !== welcome.currentId() || !isSameAppPage(event.senderUrl, url)) {
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
