/**
 * The window's own controls, as pure data.
 *
 * The desktop shell draws no title bar: the native one is hidden and only the window controls
 * remain, floating over the page. On macOS they are the traffic lights, which sit at the top of the
 * rail; elsewhere they are drawn at the top right, over a strip the page leaves for them.
 */

import type {ChromeInset} from '@vibesys/ipc';
import {DEFAULT_THEMES, THEMES} from '@vibesys/ui/tokens';

/** Match the shell's default dark theme while its document loads. */
const PAGE_BACKGROUND = THEMES[DEFAULT_THEMES.dark].colors.bg;
/** Foreground for the Windows/Linux control glyphs. */
const PAGE_FOREGROUND = THEMES[DEFAULT_THEMES.dark].colors.text;
/** Height of the Windows/Linux controls, which the page leaves a strip of across its top for. */
export const OVERLAY_HEIGHT = 40;

/**
 * macOS traffic lights are three 12px discs 8px apart. The rail holds them in a row at its top,
 * `LIGHTS_ROW` high, with the same margin before the first light and after the last, and the lights
 * centered vertically in the row.
 */
const TRAFFIC_LIGHT_SIZE = 12;
const TRAFFIC_LIGHT_GAP = 8;
const TRAFFIC_LIGHT_MARGIN = 14;
const LIGHTS_ROW = 40;
const LIGHTS_WIDTH = 3 * TRAFFIC_LIGHT_SIZE + 2 * TRAFFIC_LIGHT_GAP;

export interface WindowChrome {
  readonly backgroundColor: string;
  readonly titleBarStyle: 'hiddenInset' | 'hidden';
  readonly trafficLightPosition?: {readonly x: number; readonly y: number};
  readonly titleBarOverlay?: {
    readonly color: string;
    readonly symbolColor: string;
    readonly height: number;
  };
}

const NO_INSET: ChromeInset = {startWidth: 0, startHeight: 0, endHeight: 0};

/**
 * What the window controls cover on `platform`. On macOS the traffic lights, with their margin on
 * both sides, in a row at the top left; elsewhere the strip across the top their overlay is drawn
 * in. Full screen hides the controls, so nothing is covered and the page uses the whole window.
 */
export function chromeInset(platform: string, fullScreen = false): ChromeInset {
  if (fullScreen) return NO_INSET;
  if (platform === 'darwin') {
    return {
      startWidth: 2 * TRAFFIC_LIGHT_MARGIN + LIGHTS_WIDTH,
      startHeight: LIGHTS_ROW,
      endHeight: 0,
    };
  }
  return {startWidth: 0, startHeight: 0, endHeight: OVERLAY_HEIGHT};
}

/** The chrome options for `BrowserWindow` on `platform` (a `process.platform` value). */
export function windowChrome(platform: string): WindowChrome {
  if (platform === 'darwin') {
    return {
      backgroundColor: PAGE_BACKGROUND,
      titleBarStyle: 'hiddenInset',
      trafficLightPosition: {
        x: TRAFFIC_LIGHT_MARGIN,
        y: (LIGHTS_ROW - TRAFFIC_LIGHT_SIZE) / 2,
      },
    };
  }
  return {
    backgroundColor: PAGE_BACKGROUND,
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: PAGE_BACKGROUND,
      symbolColor: PAGE_FOREGROUND,
      height: OVERLAY_HEIGHT,
    },
  };
}
