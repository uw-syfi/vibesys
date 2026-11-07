import type {ViewerBridge} from './viewer-codec.js';

declare global {
  /** The preload's viewer bridge (`vibesysViewer`); absent outside the app. */
  var vibesysViewer: ViewerBridge | undefined;
}
