/**
 * One renderer port per call, for every channel that carries one. A call is authorized before
 * anything is provisioned or dialed, owns an abort signal that every way the caller can go away
 * ends (the port closing, a message on it, the sender destroyed, the page navigating), and is
 * answered with exactly one terminal reply, after which the port is closed.
 */
import {gatewayEnd} from '@vibesys/ipc';
import {grpcCode} from '@vibesys/transport';
import {rendererGeneration, type WelcomeInvokeEvent} from './welcome-ipc.js';

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

export function servePort(
  ipc: PortIpc,
  channel: string,
  admits: (event: WelcomeInvokeEvent) => boolean,
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
      authorize(event, admits);
      await call(value, abort.signal, reply => {
        if (!done) port.postMessage(reply);
      });
    })().then(() => finish(), finish);
  });
}

/** One terminal reply: the failure's text and, when the gateway refused the call, its gRPC status. */
export function endOf(error?: unknown): Uint8Array {
  const status = grpcCode(error);
  const message =
    error === undefined
      ? undefined
      : status !== undefined && error instanceof Error
        ? error.message
        : String(error);
  return gatewayEnd(message, status);
}

function authorize(event: PortEvent, admits: (event: WelcomeInvokeEvent) => boolean): void {
  if (
    event.senderFrame !== event.sender.mainFrame ||
    !admits({
      senderId: event.sender.id,
      senderUrl: event.senderFrame?.url ?? null,
      rendererGeneration: rendererGeneration(event.senderFrame),
    })
  )
    throw new Error('not an app-owned shell document');
}
