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
import electron = require('electron');
import type {DesktopPreload, DesktopSocket, DesktopSocketHandlers, MainToPage, PageToMain} from '@vibesys/ipc';

const CONNECT_CHANNEL: typeof import('@vibesys/ipc').CONNECT_CHANNEL = 'vibesys:connect';
const WAKE_CHANNEL: typeof import('@vibesys/ipc').WAKE_CHANNEL = 'vibesys:wake';

function connect(handlers: DesktopSocketHandlers): DesktopSocket {
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

const preload: DesktopPreload = {platform: process.platform, connect, onWake};

electron.contextBridge.exposeInMainWorld('vibesysDesktop', preload);
