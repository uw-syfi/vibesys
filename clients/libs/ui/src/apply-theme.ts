import type {ThemeName} from './tokens/index.js';

/** The one thing `applyTheme` needs of an element, so a test can pass a fake. */
export interface ThemeTarget {
  setAttribute(name: 'data-theme', value: ThemeName): void;
}

/** Shows the page in `name`: `tokens.css` has one `:root[data-theme="<name>"]` block per theme. */
export function applyTheme(name: ThemeName, target: ThemeTarget = document.documentElement): void {
  target.setAttribute('data-theme', name);
}
