/** Appearance: System follows the OS through light-dark(); Light and Dark set data-theme on <html>. */
import {useState} from 'react';

export type ThemeChoice = 'system' | 'light' | 'dark';

export const THEMES: readonly ThemeChoice[] = ['system', 'light', 'dark'];
export const THEME_LABELS: Readonly<Record<ThemeChoice, string>> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};

const KEY = 'vibesys.theme';

const parseTheme = (value: string | null): ThemeChoice | null =>
  THEMES.find(theme => theme === value) ?? null;

/** `?theme=` wins (reviews and captures), then the saved choice, then System. */
export function initialTheme(query: string | null, saved: string | null): ThemeChoice {
  return parseTheme(query) ?? parseTheme(saved) ?? 'system';
}

export function applyTheme(
  root: Pick<Element, 'setAttribute' | 'removeAttribute'>,
  choice: ThemeChoice,
): void {
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
}

/** Storage can be unavailable (private windows, blocked site data); the choice then lasts the page. */
export function savedTheme(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function saveTheme(choice: ThemeChoice): void {
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    // The choice still applies until the page reloads.
  }
}

/** Applies and persists a theme choice; shared by the run window and the home page. */
export function useTheme(opened: ThemeChoice): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [theme, setTheme] = useState(opened);
  const choose = (choice: ThemeChoice) => {
    setTheme(choice);
    applyTheme(document.documentElement, choice);
    saveTheme(choice);
    // A choice made here outranks the page's ?theme= from now on, reloads included.
    const url = new URL(location.href);
    if (url.searchParams.has('theme')) {
      url.searchParams.delete('theme');
      history.replaceState(history.state, '', url);
    }
  };
  return [theme, choose];
}
