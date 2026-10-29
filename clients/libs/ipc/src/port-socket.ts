/**
 * The page's end of one run connection over a `MessagePort` the main process relays
 * (`bridge.ts`): byte credit both ways, `open` once, one `message` per protocol frame, `close`
 * exactly once. The run preload keeps its own copy of this protocol (a sandboxed preload cannot
 * import a module); this one serves a page that receives the port itself, the app shell, which gets
 * it as a window message (`RUN_PORT_MESSAGE`). Any message the protocol does not allow closes the
 * connection.
 */
import type {DesktopSocket, DesktopSocketHandlers, MainToPage, PageToMain} from './bridge.js';

/** The page's side of the channel: what a `MessagePort` offers. */
export interface PagePort {
  onmessage: ((event: {readonly data: unknown}) => void) | null;
  postMessage(message: PageToMain): void;
  close(): void;
}

/** A frame's UTF-8 length plus its newline, as the relay counts it (neutral code has no encoder). */
function frameBytes(data: string): number {
  let bytes = 1;
  for (const character of data) {
    const code = character.codePointAt(0) ?? 0;
    bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return bytes;
}

function validCredit(bytes: unknown): bytes is number {
  return typeof bytes === 'number' && Number.isSafeInteger(bytes) && bytes > 0;
}

class PortConnection implements DesktopSocket {
  readonly #port: PagePort;
  readonly #handlers: DesktopSocketHandlers;
  readonly #queued: Array<{readonly data: string; readonly bytes: number}> = [];
  #queuedBytes = 0;
  #windowBytes = 0;
  #sendCredit = 0;
  #receiveCredit = 0;
  #opened = false;
  #closed = false;

  constructor(port: PagePort, handlers: DesktopSocketHandlers) {
    this.#port = port;
    this.#handlers = handlers;
    port.onmessage = event => this.#receive(event.data);
  }

  send(data: string): void {
    if (this.#closed) return;
    if (!this.#opened || typeof data !== 'string') {
      this.#fail();
      return;
    }
    const bytes = frameBytes(data);
    if (bytes > this.#windowBytes - this.#queuedBytes) {
      this.#fail();
      return;
    }
    this.#queued.push({data, bytes});
    this.#queuedBytes += bytes;
    this.#flush();
  }

  close(): void {
    if (this.#closed) return;
    this.#post({type: 'close'});
    this.#closed = true;
    this.#discardQueue();
    this.#port.close();
  }

  #receive(value: unknown): void {
    if (this.#closed) return;
    if (typeof value !== 'object' || value === null) {
      this.#fail();
      return;
    }
    const message = value as MainToPage;
    if (message.type === 'open') this.#open(message.creditBytes);
    else if (message.type === 'message') this.#message(message.data, message.bytes);
    else if (message.type === 'credit') this.#credited(message.bytes);
    else if (message.type === 'close') this.#ended();
    else this.#fail();
  }

  #open(creditBytes: number): void {
    if (this.#opened || !validCredit(creditBytes)) {
      this.#fail();
      return;
    }
    this.#opened = true;
    this.#windowBytes = creditBytes;
    this.#sendCredit = creditBytes;
    this.#grant(creditBytes);
    if (!this.#closed) this.#handlers.onOpen();
  }

  #message(data: string, bytes: number): void {
    if (
      !this.#opened ||
      typeof data !== 'string' ||
      !validCredit(bytes) ||
      bytes !== frameBytes(data) ||
      bytes > this.#receiveCredit
    ) {
      this.#fail();
      return;
    }
    this.#receiveCredit -= bytes;
    try {
      this.#handlers.onMessage(data);
    } finally {
      if (!this.#closed) this.#grant(bytes);
    }
  }

  #grant(bytes: number): void {
    if (this.#closed || bytes > this.#windowBytes - this.#receiveCredit) {
      this.#fail();
      return;
    }
    this.#receiveCredit += bytes;
    this.#post({type: 'credit', bytes});
  }

  #credited(bytes: number): void {
    if (!this.#opened || !validCredit(bytes) || bytes > this.#windowBytes - this.#sendCredit) {
      this.#fail();
      return;
    }
    this.#sendCredit += bytes;
    this.#flush();
  }

  #flush(): void {
    while (!this.#closed) {
      const frame = this.#queued[0];
      if (frame === undefined || frame.bytes > this.#sendCredit) return;
      this.#queued.shift();
      this.#queuedBytes -= frame.bytes;
      this.#sendCredit -= frame.bytes;
      if (!this.#post({type: 'send', data: frame.data})) return;
    }
  }

  #post(message: PageToMain): boolean {
    try {
      this.#port.postMessage(message);
      return true;
    } catch {
      this.#ended();
      return false;
    }
  }

  #fail(): void {
    if (this.#closed) return;
    this.#post({type: 'close'});
    this.#ended();
  }

  #ended(): void {
    if (this.#closed) return;
    this.#closed = true;
    this.#discardQueue();
    this.#port.close();
    this.#handlers.onClose();
  }

  #discardQueue(): void {
    this.#queued.length = 0;
    this.#queuedBytes = 0;
  }
}

/** One connection over `port`, reporting to `handlers`. */
export function openPortSocket(port: PagePort, handlers: DesktopSocketHandlers): DesktopSocket {
  return new PortConnection(port, handlers);
}
