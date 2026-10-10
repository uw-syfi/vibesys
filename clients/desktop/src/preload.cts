/**
 * Sandboxed preload: the page's only way out of its sandbox.
 *
 * It exposes the platform (data), `connect` (open one protocol connection to the server the main
 * process bound this window to; the page names no host, path, or command), and `onWake` (the
 * machine woke from sleep). A connection is a MessagePort handed to the main process, which relays
 * it to the server; the port itself stays here, in the isolated world, and the page holds only the
 * two functions below. Sandboxed preloads cannot be ES modules or import local files, hence `.cts`
 * and the channel names repeated from `bridge-protocol.ts`, whose types keep them in step.
 */
import electron = require('electron');
import type {MainToPage, PageToMain} from './bridge-protocol.js';

const CONNECT_CHANNEL: typeof import('./bridge-protocol.js').CONNECT_CHANNEL = 'vibesys:connect';
const WAKE_CHANNEL: typeof import('./bridge-protocol.js').WAKE_CHANNEL = 'vibesys:wake';

interface SocketHandlers {
  onOpen(): void;
  onMessage(data: string): void;
  onClose(): void;
}

function connect(handlers: SocketHandlers): {send(data: string): void; close(): void} {
  const {port1: port, port2: remote} = new MessageChannel();
  let closed = false;
  const post = (message: PageToMain): void => port.postMessage(message);
  port.onmessage = event => {
    if (closed) return;
    const message = event.data as MainToPage;
    if (message.type === 'open') handlers.onOpen();
    else if (message.type === 'message') handlers.onMessage(message.data);
    else {
      closed = true;
      port.close();
      handlers.onClose();
    }
  };
  electron.ipcRenderer.postMessage(CONNECT_CHANNEL, null, [remote]);
  return {
    send: data => {
      if (!closed && typeof data === 'string') post({type: 'send', data});
    },
    close: () => {
      if (closed) return;
      closed = true;
      post({type: 'close'});
      port.close();
    },
  };
}

function onWake(listener: () => void): void {
  electron.ipcRenderer.on(WAKE_CHANNEL, () => listener());
}

electron.contextBridge.exposeInMainWorld('vibesysDesktop', {
  platform: process.platform,
  connect,
  onWake,
});
