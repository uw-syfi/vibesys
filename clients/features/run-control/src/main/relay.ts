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
 * receiver has accepted and returned them. Invalid framing or credit ends the connection. A
 * complete host frame waiting on page credit, or page bytes waiting for the host stream to accept
 * them, may make no progress for at most `RELAY_PROGRESS_DEADLINE_MS`; past it the relay ends
 * unconditionally, the same way a stalled write toward either peer ends `server.relay` at its own
 * write deadline. Keeping the stream flowing until bounded pending output exists also lets a clean
 * EOF behind an exhausted credit window reach the relay.
 */
import type {Duplex} from 'node:stream';
import type {ServerMessage} from '@vibesys/contracts';
import {BRIDGE_CREDIT_WINDOW_BYTES, type MainToPage, parsePageMessage} from '@vibesys/ipc';
import {defaultScheduleTimeout, NewlineFramer, type ScheduleTimeout} from '@vibesys/transport';

/**
 * How long either direction may wait without delivery progress before the relay ends the connection
 * unconditionally.
 *
 * Mirrors `server.relay`'s `BridgeLimits.write_deadline_seconds` (also the WebSocket gateway's
 * ceiling): neither a page that stops granting credit nor a host stream that stops accepting page
 * bytes should pin the stream and message port longer than a dead peer does on those other paths.
 * Progress (the page granting credit or the host completing a write) pushes the corresponding
 * deadline back out rather than charging total elapsed time.
 */
const RELAY_PROGRESS_DEADLINE_MS = 40_000;

/** The page side of one connection. */
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
 *
 * The relay owns the signal passed to `dial`: ending either side cancels a dial still waiting and
 * the channel it opened. `scheduleTimeout` is the seam for both progress deadlines; tests inject a
 * deterministic clock, production takes the real timer.
 */
export function relay(
  port: RelayPort,
  dial: (signal: AbortSignal) => Promise<Duplex>,
  scheduleTimeout: ScheduleTimeout = defaultScheduleTimeout,
): Promise<void> {
  return new Promise(resolve => new StreamRelay(port, dial, resolve, scheduleTimeout).start());
}

class StreamRelay {
  readonly #port: RelayPort;
  readonly #dial: (signal: AbortSignal) => Promise<Duplex>;
  /** The dial and the channel it opens live exactly as long as this relay. */
  readonly #dialAbort = new AbortController();
  readonly #resolve: () => void;
  readonly #scheduleTimeout: ScheduleTimeout;
  readonly #framer = new NewlineFramer();
  readonly #pending: PendingFrame[] = [];
  /** A transport framing fault staged behind every complete host frame already accepted. */
  #framingFault: PendingFrame | null = null;
  #pendingBytes = 0;
  #pageSendCredit = BRIDGE_CREDIT_WINDOW_BYTES;
  #pageReceiveCredit = 0;
  #stream: Duplex | null = null;
  #streamEnded = false;
  #dialSettled = false;
  #closed = false;
  #settled = false;
  /** Cancels the deadline for page bytes still awaiting a host-stream write callback. */
  #hostWriteDeadline: (() => void) | null = null;
  /** Cancels the pending-page-output deadline armed by `#armCreditDeadline`, while one is live. */
  #creditDeadline: (() => void) | null = null;

  constructor(
    port: RelayPort,
    dial: (signal: AbortSignal) => Promise<Duplex>,
    resolve: () => void,
    scheduleTimeout: ScheduleTimeout,
  ) {
    this.#port = port;
    this.#dial = dial;
    this.#resolve = resolve;
    this.#scheduleTimeout = scheduleTimeout;
  }

  start(): void {
    this.#port.onClose(() => this.#finish());
    this.#port.onMessage(data => this.#receive(data));
    let dialing: Promise<Duplex>;
    try {
      dialing = this.#dial(this.#dialAbort.signal);
    } catch {
      this.#dialSettled = true;
      this.#finish();
      return;
    }
    void dialing.then(
      stream => {
        this.#dialSettled = true;
        this.#opened(stream);
        this.#settle();
      },
      () => {
        this.#dialSettled = true;
        this.#finish();
        this.#settle();
      },
    );
  }

  #opened(stream: Duplex): void {
    if (this.#closed) {
      stream.destroy();
      return;
    }
    this.#stream = stream;
    stream.pause();
    // Take ownership of terminal events before inspecting state. A stream may have ended while the
    // dial promise was handing it to us; publishing `open` for that stream would strand the page.
    stream.once('end', () => this.#endStream());
    stream.once('close', () => this.#streamClosed());
    stream.once('error', () => this.#finish());
    if (stream.destroyed || stream.closed || stream.readableEnded || stream.errored !== null) {
      this.#finish();
      return;
    }
    stream.setEncoding('utf8');
    stream.on('data', (chunk: string) => this.#receiveStreamChunk(chunk));
    if (this.#post({type: 'open', creditBytes: BRIDGE_CREDIT_WINDOW_BYTES})) {
      // Read ahead only until bounded output is waiting. In particular, do not require the first
      // credit grant to observe a host that wrote its final frame and clean EOF immediately.
      this.#flushPageOutput();
    }
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
    const writesWereDrained = this.#pageSendCredit === BRIDGE_CREDIT_WINDOW_BYTES;
    this.#pageSendCredit -= bytes;
    if (writesWereDrained && !this.#armHostWriteDeadline()) return;
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
    if (this.#pageSendCredit === BRIDGE_CREDIT_WINDOW_BYTES) this.#cancelHostWriteDeadline();
    else if (!this.#armHostWriteDeadline()) return;
    this.#post({type: 'credit', bytes});
  }

  /** Restart the no-progress bound after the host accepts some, but not all, outstanding bytes. */
  #armHostWriteDeadline(): boolean {
    this.#cancelHostWriteDeadline();
    const cancel = this.#scheduleProgressDeadline(() => {
      this.#hostWriteDeadline = null;
      this.#finish();
    });
    if (cancel === null) return false;
    this.#hostWriteDeadline = cancel;
    return true;
  }

  #cancelHostWriteDeadline(): void {
    this.#hostWriteDeadline?.();
    this.#hostWriteDeadline = null;
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
      const frame = this.#nextPendingFrame();
      if (frame === undefined || frame.bytes > this.#pageReceiveCredit) break;
      this.#pending.shift();
      this.#pendingBytes -= frame.bytes;
      this.#pageReceiveCredit -= frame.bytes;
      if (!this.#post({type: 'message', data: frame.data, bytes: frame.bytes})) return;
    }
    const stream = this.#stream;
    if (this.#pending.length === 0) {
      this.#cancelCreditDeadline();
      if (this.#streamEnded) {
        this.#finish();
        return;
      }
      // Keep reading even when current page credit is zero. The framer and pending-byte limit bound
      // this read-ahead; it is what lets a tail and clean EOF already buffered by Node become
      // observable instead of remaining forever behind a paused readable.
      if (stream !== null && !this.#closed) stream.resume();
      return;
    }

    // A complete frame is waiting for page credit. Pause before accepting another frame, and bound
    // the no-progress interval locally. This also covers a clean EOF buffered behind the frame:
    // Node need not emit `end` for the relay to own deterministic cleanup.
    if (stream !== null && !this.#streamEnded) stream.pause();
    this.#armCreditDeadline();
  }

  /** Append a terminal framing fault only after every complete peer frame. */
  #nextPendingFrame(): PendingFrame | undefined {
    const pending = this.#pending[0];
    if (pending !== undefined || !this.#streamEnded || this.#framingFault === null) return pending;
    const fault = this.#framingFault;
    this.#framingFault = null;
    this.#pending.push(fault);
    this.#pendingBytes += fault.bytes;
    return fault;
  }

  /** (Re)start the pending-page-output deadline after a stall or some insufficient progress. */
  #armCreditDeadline(): boolean {
    this.#cancelCreditDeadline();
    const cancel = this.#scheduleProgressDeadline(() => {
      this.#creditDeadline = null;
      this.#finish();
    });
    if (cancel === null) return false;
    this.#creditDeadline = cancel;
    return true;
  }

  #cancelCreditDeadline(): void {
    this.#creditDeadline?.();
    this.#creditDeadline = null;
  }

  /** Acquire one deadline without letting a reentrant or failed scheduler strand the relay. */
  #scheduleProgressDeadline(expire: () => void): (() => void) | null {
    try {
      const cancel = this.#scheduleTimeout(expire, RELAY_PROGRESS_DEADLINE_MS);
      // A structural scheduler may invoke `expire` before returning its cancellation handle. The
      // callback has already closed the relay, so retire the newly returned handle immediately.
      if (this.#closed) {
        cancel();
        return null;
      }
      return cancel;
    } catch {
      this.#finish();
      return null;
    }
  }

  #endStream(): void {
    if (this.#closed || this.#streamEnded) return;
    try {
      this.#framer.finish();
    } catch (error) {
      const data = JSON.stringify({
        type: 'protocol_error',
        code: 'invalid_frame',
        message: error instanceof Error ? error.message : String(error),
      } satisfies Extract<ServerMessage, {type: 'protocol_error'}>);
      this.#framingFault = {data, bytes: frameBytes(data)};
    }
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
    this.#dialAbort.abort();
    this.#cancelHostWriteDeadline();
    this.#cancelCreditDeadline();
    this.#pending.length = 0;
    this.#framingFault = null;
    this.#pendingBytes = 0;
    this.#stream?.destroy();
    try {
      this.#port.post({type: 'close'});
    } catch {}
    this.#port.close();
    this.#settle();
  }

  /** The relay owns a pending dial too, so completion waits until cancellation has reached it. */
  #settle(): void {
    if (!this.#closed || !this.#dialSettled || this.#settled) return;
    this.#settled = true;
    this.#resolve();
  }
}

/** Credits include the newline the byte-stream side owns. */
function frameBytes(data: string): number {
  return Buffer.byteLength(data, 'utf8') + 1;
}
