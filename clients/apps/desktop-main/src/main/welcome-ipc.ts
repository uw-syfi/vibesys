/**
 * The welcome view's requests, answered at the boundary.
 *
 * The welcome view is a trusted app page, but its requests are still input: every request is
 * parsed by `@vibesys/ipc`'s `parseWelcomeRequest`, which validates every value (host keys, paths,
 * ids, task names) before anything reaches a host, or throws naming what is wrong.
 * `WelcomeRouter` answers the channels only for each window's own welcome web contents.
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
    // The connection's own cause, typed, so a screen words it and keeps the raw text for a tooltip.
    ...(error instanceof ConnectionError ? {cause: error.reason} : {}),
  };
}

/** What one window's trusted surface asks of the app, and how its documents lose authority. */
export interface WelcomeBinding {
  /** The window's welcome surface: which WebContents is current, and when its documents change. */
  readonly document: WelcomeDocument;
  /** The page that surface loads; a sender on another page is refused. */
  readonly url: string;
  readonly answer: (request: WelcomeRequest, rendererGeneration: string) => Promise<unknown>;
  /** Settle a replaced document's asynchronous cleanup; a rejection still counts as settled. */
  readonly retire: (rendererGeneration: string) => Promise<void>;
}

/**
 * One window's admission: its documents' generations, the epoch every replacement advances (even
 * when Electron cannot identify the retiring frame), and the barrier a replacement document waits
 * on until every prior identity's cleanup has settled. A `retire` implementation owns reporting
 * its cleanup failures; rejection still counts as settlement and cannot skip a sibling generation
 * or poison a later document's barrier.
 */
class WindowAdmission {
  readonly #binding: WelcomeBinding;
  #alive = true;
  #documentEpoch = 0;
  #activeGenerations = new Set<string>();
  readonly #retiredGenerations = new Set<string>();
  #retirement = Promise.resolve();

  constructor(binding: WelcomeBinding, destroyed: () => void) {
    this.#binding = binding;
    binding.document.onNewDocument(generation => this.#retireCurrent(generation));
    binding.document.onDestroyed(() => {
      this.#alive = false;
      this.#retireCurrent();
      destroyed();
    });
  }

  /** Whether `event` came from this window's current welcome document on its page. */
  admits(event: WelcomeInvokeEvent): boolean {
    return (
      event.senderId === this.#binding.document.currentId() &&
      isSameAppPage(event.senderUrl, this.#binding.url)
    );
  }

  async handle(
    channel: string,
    event: WelcomeInvokeEvent,
    args: readonly unknown[],
  ): Promise<WelcomeResult<unknown>> {
    const requestGeneration = event.rendererGeneration;
    const requestEpoch = this.#documentEpoch;
    try {
      if (requestGeneration === null) throw new Error('welcome view document is unavailable');
      await this.#retirement;
      if (
        !this.#alive ||
        requestEpoch !== this.#documentEpoch ||
        this.#retiredGenerations.has(requestGeneration)
      )
        throw new Error('welcome view was replaced');
      this.#activeGenerations.add(requestGeneration);
      return {
        ok: true,
        value: await this.#binding.answer(parseWelcomeRequest(channel, args), requestGeneration),
      };
    } catch (error) {
      return failure(error);
    }
  }

  #retireCurrent(retiringGeneration: string | null = null): void {
    this.#documentEpoch += 1;
    if (retiringGeneration !== null) this.#activeGenerations.add(retiringGeneration);
    const retiring = [...this.#activeGenerations];
    this.#activeGenerations = new Set();
    for (const generation of retiring) {
      this.#retiredGenerations.add(generation);
    }
    this.#retirement = this.#retirement.then(async () => {
      await Promise.allSettled(retiring.map(async generation => this.#binding.retire(generation)));
    });
  }
}

/**
 * The welcome channels, answered for every window's trusted surface. Electron allows one handler
 * per channel, so the router registers each channel once and routes every request by its sender:
 * to the window whose current welcome document sent it, on that window's page; any other sender is
 * refused. Each request carries the invoking frame's process/routing identity, and each window's
 * documents lose authority on their own (`WindowAdmission`). A window's binding ends when its
 * native window closes.
 */
export class WelcomeRouter {
  readonly #windows = new Set<WindowAdmission>();

  constructor(ipc: WelcomeIpc) {
    for (const channel of Object.values(WELCOME_CHANNELS)) {
      ipc.handle(channel, async (event, ...args) => {
        const window = [...this.#windows].find(each => each.admits(event));
        if (window === undefined) return failure(new Error('not the welcome view'));
        return window.handle(channel, event, args);
      });
    }
  }

  /** Answer `binding`'s window from now until its native window closes. */
  bind(binding: WelcomeBinding): void {
    const window: WindowAdmission = new WindowAdmission(binding, () =>
      this.#windows.delete(window),
    );
    this.#windows.add(window);
  }
}
