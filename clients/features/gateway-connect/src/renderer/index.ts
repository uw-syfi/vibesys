// The replay page's "Live gateway URL" form: its view, the store that exchanges a pasted capability
// through the `bootstrap` the composition injects, and the gateway URL policy (where a page's live
// gateway is and what may appear in browser-visible URLs). The form's core (`state/`) is pure, but
// it parses with the WHATWG `URL`, a DOM global, so the feature publishes no `neutral` entry.
export {createGatewayConnectStore} from './effects/gateway-connect.js';
export {
  browserSessionForGateway,
  type GatewayBootstrapTarget,
  type GatewayConnection,
  type GatewayTarget,
  resolveGatewayTarget,
  scrubLaunchCapability,
  serializeBrowserSession,
  targetFromCapability,
  webSocketUrlWithSession,
} from './state/gateway.js';
export type {
  GatewayConnectAction,
  GatewayConnectFeedback,
  GatewayConnectState,
} from './state/gateway-connect.js';
export {GatewayForm} from './ui/gateway-form.js';
