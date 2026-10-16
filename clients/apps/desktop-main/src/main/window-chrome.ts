/**
 * The window's title-bar chrome, as pure data.
 *
 * The web UI draws its own top strip (`--page-background`, `--desktop-titlebar-height` in
 * `clients/apps/web/src/styles.css`), so the native title bar is hidden and only the window controls
 * remain, floating over that strip. The web UI has a single dark theme, so the colors do not vary
 * with the operating system's appearance; keep them equal to the web tokens.
 */

/** `--page-background` in `clients/apps/web/src/styles.css`. */
const PAGE_BACKGROUND = '#0c1018';
/** The web UI's `color` on `:root`; used for the Windows/Linux control glyphs. */
const PAGE_FOREGROUND = '#e9edf5';
/** `--desktop-titlebar-height` in `clients/apps/web/src/styles.css`. */
export const TITLEBAR_HEIGHT = 40;

/** macOS traffic lights are three 12px discs 8px apart; this centers them vertically in the strip. */
const TRAFFIC_LIGHT_SIZE = 12;
const TRAFFIC_LIGHT_GAP = 8;
const TRAFFIC_LIGHT_INSET = 16;

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

/** The top-left rectangle the window controls cover, which a page keeps clear, in CSS pixels. */
export interface ChromeInset {
  readonly width: number;
  readonly height: number;
}

/**
 * What the window controls cover on `platform`: on macOS the traffic lights, from the window's
 * edge to the last light plus the same margin again, over the strip's height; elsewhere nothing,
 * since the title-bar overlay draws the controls at the top right of the strip the pages already
 * leave.
 */
export function chromeInset(platform: string): ChromeInset {
  if (platform !== 'darwin') return {width: 0, height: 0};
  const lights = 3 * TRAFFIC_LIGHT_SIZE + 2 * TRAFFIC_LIGHT_GAP;
  return {width: 2 * TRAFFIC_LIGHT_INSET + lights, height: TITLEBAR_HEIGHT};
}

/** The chrome options for `BrowserWindow` on `platform` (a `process.platform` value). */
export function windowChrome(platform: string): WindowChrome {
  if (platform === 'darwin') {
    return {
      backgroundColor: PAGE_BACKGROUND,
      titleBarStyle: 'hiddenInset',
      trafficLightPosition: {
        x: TRAFFIC_LIGHT_INSET,
        y: (TITLEBAR_HEIGHT - TRAFFIC_LIGHT_SIZE) / 2,
      },
    };
  }
  return {
    backgroundColor: PAGE_BACKGROUND,
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: PAGE_BACKGROUND,
      symbolColor: PAGE_FOREGROUND,
      height: TITLEBAR_HEIGHT,
    },
  };
}
