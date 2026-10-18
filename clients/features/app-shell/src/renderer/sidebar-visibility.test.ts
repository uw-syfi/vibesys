import {describe, expect, test} from 'bun:test';
import {
  isSidebarShortcut,
  parseSidebarShown,
  type ShortcutKey,
  type SidebarAction,
  sidebarShortcutLabel,
  storeSidebarShown,
} from '../state/index.js';
import {
  bindSidebarShortcut,
  browserSidebarPreference,
  createSidebarStore,
  type KeyTarget,
  type ShellBridge,
  type SidebarPreference,
} from './index.js';

const NONE = {metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, repeat: false};

/** Every combination of the five flags, with the keys that are and are not the shortcut's. */
function* presses(): Generator<ShortcutKey> {
  for (const bits of Array.from({length: 32}, (_, bit) => bit)) {
    const flag = (index: number): boolean => (bits >> index) % 2 === 1;
    for (const key of ['\\', ',', '|', 'b', 'Backslash', 's']) {
      yield {
        key,
        metaKey: flag(0),
        ctrlKey: flag(1),
        altKey: flag(2),
        shiftKey: flag(3),
        repeat: flag(4),
      };
    }
  }
}

describe('the toggle shortcut', () => {
  test('property: it is the platform modifier and backslash, alone and not held', () => {
    for (const mac of [true, false]) {
      for (const event of presses()) {
        const expected =
          event.key === '\\' &&
          !event.altKey &&
          !event.shiftKey &&
          !event.repeat &&
          (mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey);
        expect(isSidebarShortcut(event, mac)).toBe(expected);
      }
    }
  });

  test('it is named for the platform', () => {
    expect(sidebarShortcutLabel(true)).toBe('⌘\\');
    expect(sidebarShortcutLabel(false)).toBe('Ctrl+\\');
  });

  test('bound to a window, it toggles the sidebar and swallows only its own key', () => {
    const listeners = new Set<(event: KeyboardEvent) => void>();
    const target: KeyTarget = {
      addEventListener: (_type, listener) => void listeners.add(listener),
      removeEventListener: (_type, listener) => void listeners.delete(listener),
    };
    const dispatched: SidebarAction[] = [];
    const stop = bindSidebarShortcut(target, true, action => dispatched.push(action));
    const press = (key: string, modifiers: Partial<ShortcutKey>): boolean => {
      let prevented = false;
      const event = {...NONE, key, ...modifiers, preventDefault: () => (prevented = true)};
      for (const listener of listeners) listener(event as unknown as KeyboardEvent);
      return prevented;
    };
    expect(press('\\', {metaKey: true})).toBe(true);
    expect(press('b', {metaKey: true})).toBe(false);
    expect(press(',', {metaKey: true})).toBe(false);
    expect(press('\\', {ctrlKey: true})).toBe(false);
    expect(dispatched).toEqual([{type: 'toggle-sidebar'}]);
    stop();
    expect(listeners.size).toBe(0);
  });
});

describe('the stored preference', () => {
  test('property: what is stored reads back, and anything else reads as nothing stored', () => {
    for (const shown of [true, false]) {
      expect(parseSidebarShown(storeSidebarShown(shown))).toBe(shown);
    }
    for (const other of [null, '', 'true', 'SHOWN', '{}', 'hidden ']) {
      expect(parseSidebarShown(other)).toBeNull();
    }
  });

  test('it lasts in storage, and a storage that fails costs only the memory of it', () => {
    const kept = new Map<string, string>();
    const storage = {
      getItem: (key: string) => kept.get(key) ?? null,
      setItem: (key: string, value: string) => void kept.set(key, value),
    } as unknown as Storage;
    const preference = browserSidebarPreference(() => storage);
    expect(preference.read()).toBeNull();
    preference.write(false);
    expect(browserSidebarPreference(() => storage).read()).toBe(false);
    preference.write(true);
    expect(browserSidebarPreference(() => storage).read()).toBe(true);

    const broken = browserSidebarPreference(() => {
      throw new Error('storage is blocked');
    });
    expect(broken.read()).toBeNull();
    expect(() => broken.write(false)).not.toThrow();
    const missing = browserSidebarPreference(() => null);
    expect(missing.read()).toBeNull();
    expect(() => missing.write(false)).not.toThrow();
    const full = browserSidebarPreference(
      () =>
        ({
          getItem: () => null,
          setItem: () => {
            throw new Error('quota');
          },
        }) as unknown as Storage,
    );
    expect(() => full.write(false)).not.toThrow();
  });
});

/** A bridge that answers nothing, and lets the test press the application menu's item. */
function quietBridge(): {readonly bridge: ShellBridge; menu(): void} {
  const never = new Promise<never>(() => undefined);
  const ignore = (): void => undefined;
  const toggles: (() => void)[] = [];
  const bridge: ShellBridge = {
    overview: () => never,
    preferences: () => never,
    archive: () => never,
    openRunWindow: () => never,
    runDigests: () => never,
    projectRuns: () => never,
    onListing: ignore,
    onRecent: ignore,
    onPreferences: ignore,
    onToggleSidebar: listener => void toggles.push(listener),
  };
  return {
    bridge,
    menu: () => {
      for (const listener of toggles) listener();
    },
  };
}

function memory(initial: boolean | null): SidebarPreference & {readonly writes: boolean[]} {
  let stored = initial;
  const writes: boolean[] = [];
  return {
    writes,
    read: () => stored,
    write: shown => {
      stored = shown;
      writes.push(shown);
    },
  };
}

describe('the sidebar store', () => {
  test('it starts shown when nothing is stored, and as stored otherwise', () => {
    for (const [stored, shown] of [
      [null, true],
      [true, true],
      [false, false],
    ] as const) {
      const {store} = createSidebarStore(quietBridge().bridge, memory(stored));
      expect(store.getState().shown).toBe(shown);
    }
  });

  test('property: each toggle, from the sidebar or the menu, flips it and is stored once', () => {
    const preference = memory(null);
    const world = quietBridge();
    const {store} = createSidebarStore(world.bridge, preference);
    const expected: boolean[] = [];
    let shown = true;
    for (let index = 0; index < 9; index += 1) {
      if (index % 3 === 0) world.menu();
      else store.dispatch({type: 'toggle-sidebar'});
      shown = !shown;
      expected.push(shown);
      expect(store.getState().shown).toBe(shown);
    }
    expect(preference.writes).toEqual(expected);
    // A state change that is not a toggle stores nothing.
    store.dispatch({type: 'select', screen: {kind: 'settings'}});
    expect(preference.writes).toEqual(expected);
    // A new window restores what the last one left.
    const restored = createSidebarStore(quietBridge().bridge, preference).store;
    expect(restored.getState().shown).toBe(shown);
  });
});
