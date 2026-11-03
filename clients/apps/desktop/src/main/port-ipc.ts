/**
 * One renderer port per call, for every channel that carries one. A call is authorized before
 * anything is provisioned or dialed, owns an abort signal that every way the caller can go away
 * ends (the port closing, a message on it, the sender destroyed, the page navigating), and is
 * answered with exactly one terminal reply, after which the port is closed.
 */
import {DesktopLinkProblem} from '@vibesys/proto';
import {gatewayEnd} from '../core/gateway-codec.js';
import {GatewayFailed, GrpcFailed, grpcCodeOf, LinkFailed} from './errors.js';

/*
 * What the lifecycle uses of Electron, and no more: Electron's `IpcMain`, `IpcMainEvent` and
 * `MessagePortMain` satisfy these, and a test fills them with a Fake that holds the same rules.
 */

/** The main side of the port a call arrives on and is answered on. */
export interface PortEnd {
  on(event: 'message' | 'close', listener: () => void): unknown;
  start(): void;
  postMessage(message: Uint8Array): void;
  close(): void;
}

/** The document that sent the call. */
export interface PortFrame {
  readonly processId: number;
  readonly routingId: number;
  readonly url: string;
}

/** The page that sent the call, and the ways it can go away. */
export interface PortSender {
  readonly id: number;
  readonly mainFrame: unknown;
  once(event: 'destroyed', listener: () => void): unknown;
  on(
    event: 'did-start-navigation',
    listener: (event: unknown, url: string, inPage: boolean, mainFrame: boolean) => void,
  ): unknown;
  removeListener(
    event: 'destroyed' | 'did-start-navigation',
    listener: (...args: never[]) => void,
  ): unknown;
}

export interface PortEvent {
  readonly ports: readonly PortEnd[];
  readonly sender: PortSender;
  readonly senderFrame: PortFrame | null;
}

export interface PortIpc {
  on(channel: string, listener: (event: PortEvent, value: unknown) => void): unknown;
}

/** What a call does: read the request, send replies through `send`, return when it is over. */
export type PortCall = (
  value: unknown,
  signal: AbortSignal,
  send: (reply: Uint8Array) => void,
) => Promise<void>;

/** The windows this app created, and the documents it serves into them. */
export interface OwnWindows {
  /** Whether `senderId` is the web contents of a window this app created. */
  readonly has: (senderId: number) => boolean;
  /** Whether `url` is a page of this app (its `file:` or app URL), not a navigated-to site. */
  readonly isAppUrl: (url: string) => boolean;
}

/**
 * Whether a call comes from the main frame of one of this app's windows, showing one of its own
 * pages. Anything else (a subframe, another window, a page the window navigated to) is refused.
 */
export function isOwnWindow(event: PortEvent, own: OwnWindows): boolean {
  const frame = event.senderFrame;
  return (
    frame !== null &&
    frame === event.sender.mainFrame &&
    own.has(event.sender.id) &&
    own.isAppUrl(frame.url)
  );
}

export function servePort(
  ipc: PortIpc,
  channel: string,
  admits: (event: PortEvent) => boolean,
  call: PortCall,
): void {
  ipc.on(channel, (event, value: unknown) => {
    const port = event.ports[0];
    if (port === undefined) return;
    const abort = new AbortController();
    let done = false;
    const finish = (error?: unknown): void => {
      if (done) return;
      done = true;
      abort.abort();
      event.sender.removeListener('destroyed', cancelled);
      event.sender.removeListener('did-start-navigation', navigated);
      port.postMessage(endOf(error));
      port.close();
    };
    const cancelled = (): void => finish(new Error('gateway caller closed'));
    const navigated = (
      _event: unknown,
      _url: string,
      inPage: boolean,
      mainFrame: boolean,
    ): void => {
      if (!inPage && mainFrame) cancelled();
    };
    port.on('message', cancelled);
    port.on('close', cancelled);
    port.start();
    event.sender.once('destroyed', cancelled);
    event.sender.on('did-start-navigation', navigated);
    void (async () => {
      // Only a window's main frame may call, whatever else `admits` allows.
      if (event.senderFrame !== event.sender.mainFrame || !admits(event))
        throw new Error('not an app-owned window');
      await call(value, abort.signal, reply => {
        if (!done) port.postMessage(reply);
      });
    })().then(() => finish(), finish);
  });
}

/** One terminal reply: the failure's text and, when the gateway refused the call, its gRPC status. */
export function endOf(error?: unknown): Uint8Array {
  const status = grpcCodeOf(error);
  const message =
    error === undefined
      ? undefined
      : error instanceof GrpcFailed || error instanceof GatewayFailed || error instanceof LinkFailed
        ? error.message
        : String(error);
  const problem =
    error instanceof LinkFailed
      ? {
          auth: DesktopLinkProblem.AUTH,
          link: DesktopLinkProblem.LINK,
          capacity: DesktopLinkProblem.CAPACITY,
        }[error.problem]
      : undefined;
  return gatewayEnd(message, status, problem);
}
