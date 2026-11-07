import type {ThemeName} from './tokens/index.js';

/** The one thing `applyTheme` needs of an element, so a test can pass a fake. */
export interface ThemeTarget {
  getAttribute(name: 'data-theme'): string | null;
  setAttribute(name: 'data-theme' | 'data-theme-motion', value: string): void;
}

/**
 * Shows the page in `name`: `tokens.css` has one `:root[data-theme="<name>"]` block per theme. The
 * selectors name `:root`, so `target` is only meaningful as the document's root element; another
 * element gets the attribute but no colors.
 *
 * The first theme a page shows appears as it is. Every later one crossfades over `--duration-slow`
 * (`data-theme-motion`, set before the theme so the change is already animated when it happens),
 * so a page never flashes between themes and never fades in from the default at startup.
 */
export function applyTheme(name: ThemeName, target: ThemeTarget = document.documentElement): void {
  if (target.getAttribute('data-theme') !== null) target.setAttribute('data-theme-motion', '');
  target.setAttribute('data-theme', name);
}
