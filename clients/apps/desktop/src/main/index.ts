/**
 * Electron main process entry (`node` runtime). The composition root that builds the app's layers
 * and starts the window lands with the first service. It is one of the two files that may call
 * `Effect.run*` (`effect-run-only-in-entries`). Until then it publishes the gateway's main side.
 */
export {createGatewayCalls, installGatewayIpc} from './gateway.js';
export {isOwnWindow, type OwnWindows} from './port-ipc.js';
