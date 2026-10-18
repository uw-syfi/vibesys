/**
 * Whether the sidebar is shown, as pure data: how a stored preference reads, and the keyboard
 * shortcut that toggles it. The toggle itself is the sidebar store's `toggle-sidebar` action.
 */

/** What a viewer's stored preference says; `null` for nothing stored or something unrecognised. */
export function parseSidebarShown(stored: string | null): boolean | null {
  if (stored === 'shown') return true;
  if (stored === 'hidden') return false;
  return null;
}

/** The text a viewer's preference is stored as. */
export function storeSidebarShown(shown: boolean): string {
  return shown ? 'shown' : 'hidden';
}

/** The parts of a key press the shortcut reads. */
export interface ShortcutKey {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
  readonly shiftKey: boolean;
  readonly repeat: boolean;
}

/**
 * Whether `event` is the toggle shortcut: Command and backslash on a Mac, Control and backslash
 * elsewhere. No other modifier is held, and a held key does not toggle again and again.
 */
export function isSidebarShortcut(event: ShortcutKey, mac: boolean): boolean {
  const modifier = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  return modifier && !event.altKey && !event.shiftKey && !event.repeat && event.key === '\\';
}

/** The shortcut as the tooltip and the menu show it. */
export function sidebarShortcutLabel(mac: boolean): string {
  return mac ? '⌘\\' : 'Ctrl+\\';
}
