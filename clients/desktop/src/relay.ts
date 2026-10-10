/**
 * One protocol connection between the page and a server: a message port on one side, a host byte
 * stream on the other.
 *
 * The page speaks message-framed protocol (one port message per protocol message, like a WebSocket
 * frame) and the server speaks the Unix transport's newline-delimited JSON. The relay is the frame
 * adapter between them, following docs/contributing/wire-protocol.md: each page message becomes
 * one line (WP-GRANULARITY, WP-NEWLINE), and each server line, reassembled by backend-client's
 * `NewlineFramer` (WP-FRAMER), becomes one page message. It never parses the protocol itself.
 *
 * Guarantees: the page sees `open` only after the stream is up, and `close` exactly once, whoever
 * ends the connection; the stream is destroyed whenever the page side goes away. A page message
 * that could break framing (anything but a string without a newline, or a send before `open`) ends
 * the connection instead of reaching the server.
 */
import type {Duplex} from 'node:stream';
import {NewlineFramer} from '@vibesys/backend-client/node';
import {type MainToPage, parsePageMessage} from './bridge-protocol.js';

/** The page side of one connection: a started message port. */
export interface RelayPort {
  post(message: MainToPage): void;
  onMessage(listener: (data: unknown) => void): void;
  /** The page closed its end (or went away). */
  onClose(listener: () => void): void;
  close(): void;
}

/**
 * Relay between `port` and the stream `dial` opens. Resolves once the connection has ended on both
 * sides; never rejects (a failed dial is reported to the page as `close` without `open`).
 */
export function relay(port: RelayPort, dial: () => Promise<Duplex>): Promise<void> {
  return new Promise(resolve => {
    let stream: Duplex | null = null;
    let closed = false;
    const finish = (): void => {
      if (closed) return;
      closed = true;
      stream?.destroy();
      port.post({type: 'close'});
      port.close();
      resolve();
    };

    port.onClose(finish);
    port.onMessage(data => {
      if (closed) return;
      const message = parsePageMessage(data);
      if (message === null || message.type === 'close') return finish();
      if (stream === null || message.data.includes('\n')) return finish();
      stream.write(`${message.data}\n`);
    });

    dial().then(
      opened => {
        if (closed) {
          opened.destroy();
          return;
        }
        stream = opened;
        attach(opened, port, finish);
        port.post({type: 'open'});
      },
      () => finish(),
    );
  });
}

function attach(stream: Duplex, port: RelayPort, finish: () => void): void {
  const framer = new NewlineFramer();
  stream.setEncoding('utf8');
  stream.on('data', (chunk: string) => {
    let lines: string[];
    try {
      lines = framer.push(chunk);
    } catch {
      finish();
      return;
    }
    for (const line of lines) port.post({type: 'message', data: line});
  });
  stream.once('end', finish);
  stream.once('close', finish);
  stream.once('error', finish);
}
