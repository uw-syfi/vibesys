/**
 * Where each history entry was scrolled, so Back and Forward return a screen to the place the
 * reader left it, not to its top. A scrolled element is named by its class and its place among the
 * elements of that class, so a screen that React mounts again finds its own scroll.
 */

/** The scroll offsets of one entry's elements, by element name. */
export type Scrolls = Readonly<Record<string, number>>;

/** The offsets last left on each entry of a window's history, which is numbered by depth. */
export class ScrollMemory {
  readonly #left = new Map<number, Scrolls>();

  /** Notes what `entry` showed when it was left. */
  leave(entry: number, scrolls: Scrolls): void {
    this.#left.set(entry, scrolls);
  }

  /** What `entry` showed when it was last left; nothing if it never was. */
  recall(entry: number): Scrolls {
    return this.#left.get(entry) ?? {};
  }

  /** A new entry after `entry` ends the entries that were ahead of it. */
  forgetAfter(entry: number): void {
    for (const each of [...this.#left.keys()]) if (each > entry) this.#left.delete(each);
  }
}

/** The entry a history state names; a window's first entry has no state and is entry 0. */
export function entryOf(state: unknown): number {
  if (typeof state !== 'object' || state === null || !('entry' in state)) return 0;
  return typeof state.entry === 'number' && Number.isInteger(state.entry) && state.entry >= 0
    ? state.entry
    : 0;
}

/** The name of `element` among the elements that scroll, or null for one with no class. */
function nameOf(element: Element): string | null {
  const classes = element.getAttribute('class');
  if (classes === null || classes === '') return null;
  const same = [...element.ownerDocument.getElementsByClassName(classes)];
  return `${same.indexOf(element)}:${classes}`;
}

function elementNamed(document: Document, name: string): Element | null {
  const split = name.indexOf(':');
  const at = Number(name.slice(0, split));
  return document.getElementsByClassName(name.slice(split + 1))[at] ?? null;
}

/** A window's scroll memory, following its history. */
export interface ScrollKeeper {
  /** Notes what the entry shown scrolled to, before the window goes anywhere else. */
  readonly leave: () => void;
  /** The window now shows a new entry after this one, and the entries ahead of it are gone. */
  readonly advance: () => number;
}

const keepers = new WeakMap<Window, ScrollKeeper>();

/**
 * The window's scroll keeper, made on first use. On Back or Forward it returns each element of the
 * entry arrived at to where it was left, as soon as the page draws it and it is long enough. It
 * stops asking when the reader scrolls, so a reader is never moved from where they put themselves.
 */
export function scrollKeeper(window: Window): ScrollKeeper {
  const made = keepers.get(window);
  if (made !== undefined) return made;
  const memory = new ScrollMemory();
  const document = window.document;
  let entry = entryOf(window.history.state);
  // The offsets still to give back to the entry arrived at.
  let owed = new Map<string, number>();

  const give = (): void => {
    for (const [name, top] of owed) {
      const element = elementNamed(document, name);
      if (element === null) continue;
      element.scrollTop = top;
      if (element.scrollTop >= top) owed.delete(name);
    }
  };
  const leave = (): void => {
    const left: Record<string, number> = {};
    // Read from the page, not from scroll events, which wait for the next frame.
    for (const element of document.querySelectorAll('*')) {
      if (element.scrollTop <= 0) continue;
      const name = nameOf(element);
      if (name !== null) left[name] = element.scrollTop;
    }
    memory.leave(entry, left);
  };

  // The reader's own scrolling, by wheel, touch, key or scroll bar, ends what is owed.
  for (const kind of ['wheel', 'touchstart', 'keydown', 'pointerdown'])
    window.addEventListener(kind, () => owed.clear(), true);
  // First, so the entry is noted while the page still shows it.
  window.addEventListener(
    'popstate',
    () => {
      leave();
      entry = entryOf(window.history.state);
      owed = new Map(Object.entries(memory.recall(entry)));
    },
    true,
  );
  new MutationObserver(() => {
    if (owed.size > 0) give();
  }).observe(document.documentElement, {childList: true, subtree: true});

  const keeper: ScrollKeeper = {
    leave,
    advance: () => {
      memory.forgetAfter(entry);
      owed.clear();
      entry += 1;
      return entry;
    },
  };
  keepers.set(window, keeper);
  return keeper;
}
