/**
 * Sandboxed preload: the page's only way out of its sandbox.
 *
 * It exposes the platform (data), `connect` (open one protocol connection to the server the main
 * process bound this window to; the page names no host, path, or command), and `onWake` (the
 * machine woke from sleep). A connection is a MessagePort handed to the main process, which relays
 * it to the server; the port itself stays here, in the isolated world, and the page holds only the
 * two functions below. Sandboxed preloads cannot be ES modules or import local files, hence `.cts`
 * and the channel names repeated from `@vibesys/ipc`, whose types keep them in step.
 */
import type {
  DesktopPreload,
  DesktopSocket,
  DesktopSocketHandlers,
  MainToPage,
  PageToMain,
  RunChannelRole,
} from '@vibesys/ipc';

const CONNECT_CHANNEL: typeof import('@vibesys/ipc').CONNECT_CHANNEL = 'vibesys:connect';
const WAKE_CHANNEL: typeof import('@vibesys/ipc').WAKE_CHANNEL = 'vibesys:wake';

interface PreloadMessagePort {
  onmessage: ((event: MessageEvent<unknown>) => void) | null;
  postMessage(message: PageToMain): void;
  close(): void;
}

/** Effects the sandbox grants this preload, injectable so the exposed bridge has a public Fake. */
interface RunPreloadCapabilities<RemotePort> {
  readonly platform: string;
  createChannel(): {readonly port: PreloadMessagePort; readonly remote: RemotePort};
  attach(remote: RemotePort, role: RunChannelRole): void;
  onWake(listener: () => void): void;
}

/** Build the page capability from its Electron boundary effects. */
function createRunPreload<RemotePort>(
  capabilities: RunPreloadCapabilities<RemotePort>,
): DesktopPreload {
  return {
    platform: capabilities.platform,
    connect: (role, handlers) => connect(capabilities, role, handlers),
    onWake: listener => capabilities.onWake(listener),
  };
}

function connect<RemotePort>(
  capabilities: RunPreloadCapabilities<RemotePort>,
  role: RunChannelRole,
  handlers: DesktopSocketHandlers,
): DesktopSocket {
  const {port, remote} = capabilities.createChannel();
  const connection = new PortConnection(port, handlers);
  capabilities.attach(remote, role);
  return {
    send: data => connection.send(data),
    close: () => connection.close(),
  };
}

class PortConnection implements DesktopSocket {
  readonly #port: PreloadMessagePort;
  readonly #handlers: DesktopSocketHandlers;
  readonly #queued: Array<{readonly data: string; readonly bytes: number}> = [];
  #queuedBytes = 0;
  #windowBytes = 0;
  #sendCredit = 0;
  #receiveCredit = 0;
  #opened = false;
  #closed = false;

  constructor(port: PreloadMessagePort, handlers: DesktopSocketHandlers) {
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
    if (bytes > this.#windowBytes || bytes > this.#windowBytes - this.#queuedBytes) {
      this.#fail();
      return;
    }
    this.#queued.push({data, bytes});
    this.#queuedBytes += bytes;
    this.#flush();
  }

  #grant(bytes: number): void {
    if (
      this.#closed ||
      !this.#opened ||
      !validCredit(bytes) ||
      bytes > this.#windowBytes - this.#receiveCredit
    ) {
      this.#fail();
      return;
    }
    this.#receiveCredit += bytes;
    this.#post({type: 'credit', bytes});
  }

  close(): void {
    if (this.#closed) return;
    this.#post({type: 'close'});
    this.#closed = true;
    this.#discardQueue();
    this.#port.close();
  }

  #receive(value: unknown): void {
    if (this.#closed || typeof value !== 'object' || value === null) {
      if (!this.#closed) this.#fail();
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

const UTF8 = new TextEncoder();

function frameBytes(data: string): number {
  return UTF8.encode(data).byteLength + 1;
}

function validCredit(bytes: unknown): bytes is number {
  return typeof bytes === 'number' && Number.isSafeInteger(bytes) && bytes > 0;
}

// Electron's sandboxed CommonJS preload cannot import local runtime modules, so production wiring
// remains in this file. Bun tests have no Electron renderer process and exercise the factory above.
if (process.type === 'renderer') {
  const electron: typeof import('electron') = require('electron');
  electron.contextBridge.exposeInMainWorld(
    'vibesysDesktop',
    createRunPreload({
      platform: process.platform,
      createChannel: () => {
        const {port1: port, port2: remote} = new MessageChannel();
        return {port, remote};
      },
      attach: (remote, role) => electron.ipcRenderer.postMessage(CONNECT_CHANNEL, role, [remote]),
      onWake: listener => electron.ipcRenderer.on(WAKE_CHANNEL, () => listener()),
    }),
  );
}

export = createRunPreload;
