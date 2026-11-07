import type {PreferencesBridge} from './preferences-codec.js';

declare global {
  /** The preload's preferences bridge (`vibesysPreferences`); absent outside the app. */
  var vibesysPreferences: PreferencesBridge | undefined;
}
