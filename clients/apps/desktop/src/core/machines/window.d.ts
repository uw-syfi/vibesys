import type {MachinesBridge} from './machines-codec.js';

declare global {
  /** The preload's machines bridge (`vibesysMachines`); absent outside the app. */
  var vibesysMachines: MachinesBridge | undefined;
}
