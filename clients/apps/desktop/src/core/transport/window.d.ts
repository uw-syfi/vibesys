import type {GatewayPortBridge} from '@vibesys/ipc';

declare global {
  /** The preload's gateway (`vibesysGateway`); absent outside the app. */
  var vibesysGateway: GatewayPortBridge | undefined;
}
