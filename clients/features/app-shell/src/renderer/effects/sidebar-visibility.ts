/**
 * The sidebar's visibility at the window's edge: the viewer's stored preference, and the keyboard
 * shortcut that toggles it.
 */
import {
  isSettingsShortcut,
  isSidebarShortcut,
  parseSidebarShown,
  type ShortcutKey,
  type SidebarAction,
  storeSidebarShown,
} from '../../state/index.js';

/** Where whether the sidebar is shown is kept between runs of the app. */
export interface SidebarPreference {
  /** The stored choice, or null when none is stored or it cannot be read. */
  read(): boolean | null;
  /** Keep `shown`; a failure to store it is not an error, the choice just lasts this run. */
  write(shown: boolean): void;
}

export const SIDEBAR_PREFERENCE_KEY = 'vibesys.sidebar';

/**
 * The preference in the viewer's browser storage. Storage can be missing, blocked or full, so
 * every access is guarded: `storage` may throw or return null, and then nothing is kept.
 */
export function browserSidebarPreference(storage: () => Storage | null): SidebarPreference {
  return {
    read() {
      try {
        return parseSidebarShown(storage()?.getItem(SIDEBAR_PREFERENCE_KEY) ?? null);
      } catch {
        return null;
      }
    },
    write(shown) {
      try {
        storage()?.setItem(SIDEBAR_PREFERENCE_KEY, storeSidebarShown(shown));
      } catch {
        // The choice lasts this run only.
      }
    },
  };
}

/** What the shortcut listens on: a window's key presses. */
export interface KeyTarget {
  addEventListener(type: 'keydown', listener: (event: KeyboardEvent) => void): void;
  removeEventListener(type: 'keydown', listener: (event: KeyboardEvent) => void): void;
}

/** Toggle the sidebar on its shortcut (`mac`: Command, else Control); returns how to stop. */
export function bindSidebarShortcut(
  target: KeyTarget,
  mac: boolean,
  dispatch: (action: SidebarAction) => void,
): () => void {
  const listener = (event: ShortcutKey & {preventDefault(): void}): void => {
    if (isSettingsShortcut(event, mac)) {
      event.preventDefault();
      dispatch({type: 'select', screen: {kind: 'settings'}});
      return;
    }
    if (!isSidebarShortcut(event, mac)) return;
    event.preventDefault();
    dispatch({type: 'toggle-sidebar'});
  };
  target.addEventListener('keydown', listener);
  return () => target.removeEventListener('keydown', listener);
}

/** Open Settings from a run window, where the main window owns the sidebar state. */
export function bindRunWindowSettingsShortcut(
  target: KeyTarget,
  mac: boolean,
  openSettings: () => void,
): () => void {
  const listener = (event: ShortcutKey & {preventDefault(): void}): void => {
    if (!isSettingsShortcut(event, mac)) return;
    event.preventDefault();
    openSettings();
  };
  target.addEventListener('keydown', listener);
  return () => target.removeEventListener('keydown', listener);
}
