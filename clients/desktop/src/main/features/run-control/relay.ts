/**
 * One protocol connection between the page and a server: a message port on one side, a host byte
 * stream on the other.
 *
 * The page speaks message-framed protocol (one port message per protocol message, like a WebSocket
 * frame) and the server speaks the Unix transport's newline-delimited JSON. The relay is the frame
 * adapter between them, following docs/contributing/wire-protocol.md: each page message becomes
 * one line (WP-GRANULARITY, WP-NEWLINE), and each server line, reassembled by @vibesys/transport's
 * `NewlineFramer` (WP-FRAMER), becomes one page message. It never parses the protocol itself.
 *
 * Guarantees: the page sees `open` only after the stream is up, and `close` exactly once, whoever
 * ends the connection; the stream is destroyed whenever the page side goes away. Each direction
 * has one fixed UTF-8 byte-credit window. A sender cannot move more bytes across the port until the
 * receiver has accepted and returned them. Invalid framing or credit ends the connection.
 */
import type {Duplex} from 'node:stream';
import {NewlineFramer} from '@vibesys/transport';
import {
  BRIDGE_CREDIT_WINDOW_BYTES,
  type MainToPage,
  parsePageMessage,
} from '../../../common/bridge-protocol.js';

/** The page side of one connection: a started message port. */
export interface RelayPort {
  post(message: MainToPage): void;
  onMessage(listener: (data: unknown) => void): void;
  /** The page closed its end (or went away). */
  onClose(listener: () => void): void;
  close(): void;
}

interface PendingFrame {
  readonly data: string;
  readonly bytes: number;
}

/**
 * Relay between `port` and the stream `dial` opens. Resolves once the connection has ended on both
 * sides; never rejects (a failed dial is reported to the page as `close` without `open`).
 */
export function relay(port: RelayPort, dial: () => Promise<Duplex>): Promise<void> {
  return new Promise(resolve => new StreamRelay(port, dial, resolve).start());
}

class StreamRelay {
  readonly #port: RelayPort;
  readonly #dial: () => Promise<Duplex>;
  readonly #resolve: () => void;
  readonly #framer = new NewlineFramer();
  readonly #pending: PendingFrame[] = [];
  #pendingBytes = 0;
  #pageSendCredit = BRIDGE_CREDIT_WINDOW_BYTES;
  #pageReceiveCredit = 0;
  #stream: Duplex | null = null;
  #streamEnded = false;
  #closed = false;

  constructor(port: RelayPort, dial: () => Promise<Duplex>, resolve: () => void) {
    this.#port = port;
    this.#dial = dial;
    this.#resolve = resolve;
  }

  start(): void {
    this.#port.onClose(() => this.#finish());
    this.#port.onMessage(data => this.#receive(data));
    void this.#dial().then(
      stream => this.#opened(stream),
      () => this.#finish(),
    );
  }

  #opened(stream: Duplex): void {
    if (this.#closed) {
      stream.destroy();
      return;
    }
    this.#stream = stream;
    stream.pause();
    stream.setEncoding('utf8');
    stream.on('data', (chunk: string) => this.#receiveStreamChunk(chunk));
    stream.once('end', () => this.#endStream());
    stream.once('close', () => this.#streamClosed());
    stream.once('error', () => this.#finish());
    this.#post({type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES});
  }

  #receive(value: unknown): void {
    if (this.#closed) return;
    const message = parsePageMessage(value);
    if (message === null || message.type === 'close') {
      this.#finish();
      return;
    }
    if (this.#stream === null || (this.#streamEnded && message.type === 'send')) {
      this.#finish();
      return;
    }
    if (message.type === 'credit') this.#receiveCredit(message.bytes);
    else this.#sendToStream(message.data);
  }

  #receiveCredit(bytes: number): void {
    if (bytes > BRIDGE_CREDIT_WINDOW_BYTES - this.#pageReceiveCredit) {
      this.#finish();
      return;
    }
    this.#pageReceiveCredit += bytes;
    this.#flushPageOutput();
  }

  #sendToStream(data: string): void {
    const stream = this.#stream;
    if (stream === null || data.includes('\n')) {
      this.#finish();
      return;
    }
    const bytes = frameBytes(data);
    if (bytes > this.#pageSendCredit) {
      this.#finish();
      return;
    }
    this.#pageSendCredit -= bytes;
    try {
      stream.write(`${data}\n`, error => this.#streamWriteCompleted(bytes, error));
    } catch {
      this.#finish();
    }
  }

  #streamWriteCompleted(bytes: number, error: Error | null | undefined): void {
    if (this.#closed) return;
    if (error !== null && error !== undefined) {
      this.#finish();
      return;
    }
    this.#pageSendCredit += bytes;
    this.#post({type: 'credit', bytes});
  }

  #receiveStreamChunk(chunk: string): void {
    let lines: string[];
    try {
      lines = this.#framer.push(chunk);
    } catch {
      this.#finish();
      return;
    }
    for (const data of lines) {
      const bytes = frameBytes(data);
      if (bytes > BRIDGE_CREDIT_WINDOW_BYTES - this.#pendingBytes) {
        this.#finish();
        return;
      }
      this.#pending.push({data, bytes});
      this.#pendingBytes += bytes;
    }
    this.#flushPageOutput();
  }

  #flushPageOutput(): void {
    while (!this.#closed) {
      const frame = this.#pending[0];
      if (frame === undefined || frame.bytes > this.#pageReceiveCredit) break;
      this.#pending.shift();
      this.#pendingBytes -= frame.bytes;
      this.#pageReceiveCredit -= frame.bytes;
      if (!this.#post({type: 'message', data: frame.data, bytes: frame.bytes})) return;
    }
    if (this.#pending.length === 0 && this.#streamEnded) {
      this.#finish();
      return;
    }
    const stream = this.#stream;
    if (stream === null || this.#closed || this.#streamEnded) return;
    if (this.#pending.length === 0 && this.#pageReceiveCredit > 0) stream.resume();
    else stream.pause();
  }

  #endStream(): void {
    if (this.#closed || this.#streamEnded) return;
    this.#streamEnded = true;
    this.#flushPageOutput();
  }

  /** Only a clean readable EOF may wait for page credit to drain complete frames. */
  #streamClosed(): void {
    if (!this.#streamEnded) this.#finish();
  }

  #post(message: MainToPage): boolean {
    if (this.#closed) return false;
    try {
      this.#port.post(message);
      return true;
    } catch {
      this.#finish();
      return false;
    }
  }

  #finish(): void {
    if (this.#closed) return;
    this.#closed = true;
    this.#pending.length = 0;
    this.#pendingBytes = 0;
    this.#stream?.destroy();
    try {
      this.#port.post({type: 'close'});
    } catch {}
    this.#port.close();
    this.#resolve();
  }
}

/** Credits include the newline the byte-stream side owns. */
function frameBytes(data: string): number {
  return Buffer.byteLength(data, 'utf8') + 1;
}
