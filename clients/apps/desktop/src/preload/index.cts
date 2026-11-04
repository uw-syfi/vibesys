/**
 * Sandboxed preload (`preload` runtime): the page's only way out. It exposes `vibesysGateway`, a
 * byte-only bridge that opens one MessagePort to the main process per call and carries protobuf
 * bytes both ways; generated schemas and application state stay outside it. A sandboxed preload
 * cannot import local modules, so the channel name and the bridge's shape are repeated here (a
 * test holds the name to `GATEWAY_CHANNEL` and the shape to `GatewayPortBridge` in
 * `core/gateway-codec.ts`) and it loads only Electron at run time (`preload-sandbox`).
 */

/** What the preload exposes on the window; `GatewayPortBridge` in `core/gateway-codec.ts` is the page's view of it. */
interface GatewayPortBridge {
  open(
    request: Uint8Array,
    receive: (reply: Uint8Array) => void,
    failed: (error: string) => void,
  ): {cancel(): void};
}

/** The channel main serves; it must equal `GATEWAY_CHANNEL` in `core/gateway-codec.ts`, which a test holds. */
const GATEWAY_PRELOAD_CHANNEL = 'vibesys:gateway';

/** The transferred channel, injectable without granting a preload more runtime imports. */
interface GatewayPreloadChannel {
  listen(receive: (value: unknown) => void, failed: () => void): void;
  send(request: Uint8Array): void;
  close(): void;
}

/** A byte-only capability: each `open` takes a fresh channel and releases it exactly once. */
function createGatewayPreload(capabilities: {channel(): GatewayPreloadChannel}): GatewayPortBridge {
  return {
    open(request, receive, failed) {
      const channel = capabilities.channel();
      let closed = false;
      const close = (): void => {
        if (closed) return;
        closed = true;
        channel.close();
      };
      channel.listen(
        value => {
          if (closed) return;
          if (!(value instanceof Uint8Array)) {
            close();
            failed('gateway reply must be protobuf bytes');
          } else receive(value);
        },
        () => {
          if (closed) return;
          close();
          failed('gateway reply could not be delivered');
        },
      );
      try {
        channel.send(request);
      } catch (error) {
        close();
        throw error;
      }
      return {cancel: close};
    },
  };
}

module.exports = {createGatewayPreload, GATEWAY_PRELOAD_CHANNEL};

// Electron's sandboxed preload has no renderer process under test, so the factory above is what a
// test exercises and this is the production wiring.
if (process.type === 'renderer') {
  const electron: typeof import('electron') = require('electron');
  electron.contextBridge.exposeInMainWorld(
    'vibesysGateway',
    createGatewayPreload({
      channel: () => {
        const {port1, port2} = new MessageChannel();
        return {
          listen: (receive, failed) => {
            port1.onmessage = event => receive(event.data);
            port1.onmessageerror = failed;
          },
          send: request =>
            electron.ipcRenderer.postMessage(GATEWAY_PRELOAD_CHANNEL, request, [port2]),
          close: () => port1.close(),
        };
      },
    }),
  );
}
