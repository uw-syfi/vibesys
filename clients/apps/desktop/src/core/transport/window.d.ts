import type {GatewayPortBridge} from '../gateway-codec.js';

declare global {
  /** The preload's gateway (`vibesysGateway`); absent outside the app. */
  var vibesysGateway: GatewayPortBridge | undefined;
}
