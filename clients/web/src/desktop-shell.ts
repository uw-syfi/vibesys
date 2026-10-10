/**
 * Detects the Electron desktop shell and describes it to the stylesheet.
 *
 * The shell's sandboxed preload exposes `window.vibesysDesktop`; this module is the only place that
 * reads it. In a plain browser the global is absent and the page is left exactly as it was.
 */

/** The platforms the desktop shell reports (`process.platform` values it supports). */
const PLATFORMS = ['darwin', 'win32', 'linux'] as const;

type DesktopPlatform = (typeof PLATFORMS)[number];

/** What the desktop shell's preload exposes on `window`. */
interface VibesysDesktop {
  readonly platform: DesktopPlatform;
}

declare global {
  interface Window {
    vibesysDesktop?: VibesysDesktop;
  }
}

/** The `<html>` attributes that switch on desktop-only styling. */
export interface DesktopShellAttributes {
  readonly 'data-shell': 'desktop';
  readonly 'data-platform': DesktopPlatform;
}

/** The attributes for a bridge value, or `null` when it is not a recognized desktop shell. */
export function desktopShellAttributes(bridge: unknown): DesktopShellAttributes | null {
  if (typeof bridge !== 'object' || bridge === null) return null;
  const platform: unknown = (bridge as {readonly platform?: unknown}).platform;
  const known = PLATFORMS.find(candidate => candidate === platform);
  return known === undefined ? null : {'data-shell': 'desktop', 'data-platform': known};
}

/** Mark `root` for desktop-only styling when the page runs inside the desktop shell. */
export function markDesktopShell(root: HTMLElement, bridge: unknown): void {
  const attributes = desktopShellAttributes(bridge);
  if (attributes === null) return;
  for (const [name, value] of Object.entries(attributes)) root.setAttribute(name, value);
}
