/**
 * The window's own controls and how the page keeps clear of them. Every window the app opens has no
 * native title bar, so the page continues to the window's top edge and its own header is the bar.
 * macOS keeps its traffic lights, inset into the header row; the other systems keep their controls
 * as an overlay across the top right. The page learns what the controls cover from `--chrome-*`
 * custom properties on its root (`rail.css`), which full screen sets to nothing.
 */

/**
 * The smallest window the app is laid out for, in CSS pixels. Every screen fits it with no box
 * scrolling the wrong way, and the layout suites (`tools/e2e/scroll-audit.mts`) start their sweep of
 * window sizes here, so a screen is held to every size a person can drag the window to.
 */
export const MINIMUM_WINDOW = {minWidth: 480, minHeight: 600} as const;

/** What the controls cover, in CSS pixels. */
export interface WindowChrome {
  readonly startWidth: number;
  readonly startHeight: number;
  readonly endHeight: number;
}

/**
 * The three lights are 60 by 14 px: x 10 centres them in the 80 px corner (the rail's own centre
 * line), y 13 centres them in the corner's 40 px header band.
 */
const TRAFFIC_LIGHTS = {x: 10, y: 13} as const;
const MAC_CORNER = {width: 80, height: 40} as const;
const OVERLAY_HEIGHT = 40;

/** The creation options of every window the app opens, a run's report included. */
export function windowChromeOptions(platform: string): {
  readonly titleBarStyle: 'hiddenInset' | 'hidden';
  readonly trafficLightPosition?: {readonly x: number; readonly y: number};
  readonly titleBarOverlay?: {readonly height: number};
} {
  return platform === 'darwin'
    ? {titleBarStyle: 'hiddenInset', trafficLightPosition: TRAFFIC_LIGHTS}
    : {titleBarStyle: 'hidden', titleBarOverlay: {height: OVERLAY_HEIGHT}};
}

/** What the controls cover on `platform`: nothing in full screen, which hides them. */
export function windowChrome(platform: string, fullScreen: boolean): WindowChrome {
  if (fullScreen) return {startWidth: 0, startHeight: 0, endHeight: 0};
  return platform === 'darwin'
    ? {startWidth: MAC_CORNER.width, startHeight: MAC_CORNER.height, endHeight: 0}
    : {startWidth: 0, startHeight: 0, endHeight: OVERLAY_HEIGHT};
}

/** The stylesheet that tells the page what the controls cover. */
export const chromeCss = (chrome: WindowChrome): string =>
  `:root{--chrome-start-width:${chrome.startWidth}px;--chrome-start-height:${chrome.startHeight}px;--chrome-end-height:${chrome.endHeight}px}`;
