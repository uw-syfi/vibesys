import {createContext, type ReactNode, type UIEvent, useContext, useRef, useState} from 'react';
import {SIZE} from '../tokens/index.js';

/** Rows drawn beyond the visible ones on each side, so a scroll does not show a gap. */
const OVERSCAN = 6;

/** Where each row starts, and, last, the total height; `measured` holds the rows drawn so far. */
function offsetsOf(
  count: number,
  keyOf: (index: number) => string,
  estimate: number,
  measured: ReadonlyMap<string, number>,
): number[] {
  const offsets: number[] = [0];
  let at = 0;
  for (let index = 0; index < count; index++) {
    at += measured.get(keyOf(index)) ?? estimate;
    offsets.push(at);
  }
  return offsets;
}

/** The last index in `offsets` that is at or before `position`. */
function indexAt(offsets: readonly number[], position: number): number {
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if ((offsets[middle] ?? 0) <= position) low = middle;
    else high = middle - 1;
  }
  return low;
}

function atBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 2;
}

/** Whether a following list's reader has scrolled above `placed`, where it last put itself. */
function leftTheEnd(element: HTMLElement, following: boolean, placed: number): boolean {
  return following && !atBottom(element) && element.scrollTop < placed - 1;
}

/**
 * Puts a resting list back where it stood before the commit. `before` is what the scroll was when
 * the commit's render began, so whatever the reader or the list had moved it to by then is the
 * position kept. A scroll that ends the commit higher, with the position still available, was
 * clamped by a layout in the middle of the commit against a momentarily shorter list.
 */
function unclamped(element: HTMLElement, before: number | null, resting: boolean): void {
  if (!resting || before === null || element.scrollTop >= before) return;
  if (element.scrollHeight - element.clientHeight >= before) element.scrollTop = before;
}

/** Scrolls to the end and returns where that landed. */
function scrollToEnd(element: HTMLElement): number {
  element.scrollTop = element.scrollHeight;
  return element.scrollTop;
}

/** The element of the row at `index`, or null while it is not drawn. */
function drawnRow(element: HTMLElement, index: number): HTMLElement | null {
  return element.querySelector<HTMLElement>(`[aria-posinset="${index + 1}"]`);
}

/**
 * Where a list puts its scroll after a commit: at `holdAt` while it holds a row, else at its end
 * while it follows. Whether the reader has left the end of a following list (it then stops following).
 */
function placedOnCommit(
  element: HTMLElement,
  holdAt: number | null,
  following: {current: boolean},
  placed: {current: number},
): boolean {
  if (holdAt !== null) {
    if (Math.abs(element.scrollTop - holdAt) >= 1) element.scrollTop = holdAt;
    return false;
  }
  // The reader scrolled up in the frame before the scroll event: let them go.
  if (leftTheEnd(element, following.current, placed.current)) {
    following.current = false;
    return true;
  }
  if (following.current) placed.current = scrollToEnd(element);
  return false;
}

/** Hands the row a `reveal` waits for to its caller once it is drawn. */
function deliverPending(
  element: HTMLElement,
  pending: {
    current: {readonly index: number; readonly then: (row: HTMLElement) => void} | null;
  },
): void {
  const wanted = pending.current;
  if (wanted === null) return;
  const row = drawnRow(element, wanted.index);
  if (row === null) return;
  pending.current = null;
  wanted.then(row);
}

/** The scroll position that shows the row from `start` to `end` with the least movement from `top`. */
function scrollFor(start: number, end: number | undefined, top: number, height: number): number {
  const stop = end ?? start;
  if (start < top) return start;
  return stop > top + height ? stop - height : top;
}

/** Records the height `row` has now under `key`; whether that is a change. */
function remeasured(row: HTMLElement, key: string, measured: Map<string, number>): boolean {
  const height = row.offsetHeight;
  if (height === 0 || measured.get(key) === height) return false;
  measured.set(key, height);
  return true;
}

/** The place of the row at `index`: measuring it records its height and tells the list when that changed. */
function slotFor(
  index: number,
  count: number,
  keyOf: (index: number) => string,
  measured: Map<string, number>,
  changed: () => void,
): Slot {
  return {
    index,
    size: count,
    measure: row => row !== null && remeasured(row, keyOf(index), measured) && changed(),
  };
}

/** What a row of a windowed list is told about its place: for its position and its measuring. */
interface Slot {
  readonly index: number;
  readonly size: number;
  readonly measure: (row: HTMLElement | null) => void;
}

const SlotContext = createContext<Slot | null>(null);

/**
 * The place a windowed list gave the row being drawn, or null when it is drawn outside one. A
 * row sets `aria-posinset` and `aria-setsize` from it and passes `measure` as its element's ref.
 */
export function useSlot(): Slot | null {
  return useContext(SlotContext);
}

/** Draws `children` as the row at `slot` of a windowed list. */
export function InSlot(props: {readonly slot: Slot; readonly children: ReactNode}): ReactNode {
  return <SlotContext.Provider value={props.slot}>{props.children}</SlotContext.Provider>;
}

export interface Windowed {
  /** The ref of the scrolling element. A new function every render, so React calls it after every commit. */
  readonly scroller: (element: HTMLElement | null) => void;
  readonly scrolled: (event: UIEvent<HTMLElement>) => void;
  /** Called on the reader's own input (wheel, touch, key, pointer): it ends the hold of `startAt`. */
  readonly touched: () => void;
  /** The indexes drawn: `first` up to, not including, `last`. */
  readonly first: number;
  readonly last: number;
  /** The heights of the spacers that stand for the rows before and after those drawn. */
  readonly before: number;
  readonly after: number;
  readonly slotOf: (index: number) => Slot;
  /** Scrolls so the row at `index` is drawn, then lets `then` act on the drawn element. */
  readonly reveal: (index: number, then: (row: HTMLElement) => void) => void;
  /** Whether rows arrived below the visible part while the reader had scrolled up. */
  readonly behind: boolean;
  readonly jump: () => void;
}

/**
 * The state of a list that draws only the rows near its visible part. A row's height is the
 * `estimate` until it has been drawn, then the height it was measured at. With `follow`, a list
 * scrolled to its end stays at it as rows arrive and grow; scrolling up sets `behind` instead. No
 * effect hooks: the scroller's ref is called after every commit and does the work effects would.
 * Only a list that follows its end holds the end; any other is just scrolled to it. While held, the
 * scroll events are the list's own placing and the browser's anchoring.
 */
interface WindowedOptions {
  readonly count: number;
  readonly keyOf: (index: number) => string;
  readonly estimate: number;
  readonly follow: boolean;
  /** A number that, each time it changes, scrolls the list to its end. */
  readonly toEnd?: number;
  /**
   * The row the list opens at, its start at the top edge, whole: held there while rows are
   * measured, until the reader scrolls or `toEnd` changes. Without it a list that follows opens at
   * its end, which can cut the row above by any amount.
   */
  readonly startAt?: number;
}

export function useWindowed(options: WindowedOptions): Windowed {
  const {count, keyOf, estimate, follow, toEnd, startAt} = options;
  const held = useRef(startAt !== undefined && startAt >= 0 && startAt < count);
  const following = useRef(follow && !held.current);
  const scrolledToEnd = useRef(toEnd);
  const pane = useRef<HTMLElement | null>(null);
  // Where the list last put itself at its end: a reader above that has left it, even before the
  // scroll event that says so arrives.
  const placed = useRef(0);
  const measured = useRef(new Map<string, number>());
  const pending = useRef<{
    readonly index: number;
    readonly then: (row: HTMLElement) => void;
  } | null>(null);
  const [view, setView] = useState({top: 0, height: SIZE.thread});
  const [, setMeasures] = useState(0);
  const [behind, setBehind] = useState(false);

  const offsets = offsetsOf(count, keyOf, estimate, measured.current);
  const total = offsets.at(-1) ?? 0;
  const holdAt = held.current ? (offsets[startAt ?? 0] ?? 0) : null;
  const top =
    holdAt !== null ? holdAt : following.current ? Math.max(0, total - view.height) : view.top;
  const first = Math.max(0, indexAt(offsets, top) - OVERSCAN);
  const last = Math.min(count, indexAt(offsets, top + view.height) + 1 + OVERSCAN);

  // The scroll as this commit's render finds it: before any layout the commit forces.
  const before = pane.current?.scrollTop ?? null;
  const scroller = (element: HTMLElement | null): void => {
    pane.current = element;
    if (element === null) return;
    if (element.clientHeight > 0 && element.clientHeight !== view.height) {
      setView({top: view.top, height: element.clientHeight});
    }
    if (placedOnCommit(element, holdAt, following, placed)) setBehind(true);
    unclamped(element, before, holdAt === null && !follow);
    if (scrolledToEnd.current !== toEnd) {
      scrolledToEnd.current = toEnd;
      jump();
    }
    deliverPending(element, pending);
  };
  const touched = (): void => {
    const element = pane.current;
    if (!held.current || element === null) return;
    held.current = false;
    following.current = follow && atBottom(element);
    placed.current = element.scrollTop;
    setView({top: element.scrollTop, height: element.clientHeight || view.height});
  };
  const scrolled = (event: UIEvent<HTMLElement>): void => {
    const element = event.currentTarget;
    if (held.current) return;
    if (follow) {
      following.current = atBottom(element);
      setBehind(!following.current);
    }
    setView({top: element.scrollTop, height: element.clientHeight || view.height});
  };
  const jump = (): void => {
    held.current = false;
    if (follow) {
      following.current = true;
      setBehind(false);
    }
    if (pane.current !== null) placed.current = scrollToEnd(pane.current);
  };
  const reveal = (index: number, then: (row: HTMLElement) => void): void => {
    const element = pane.current;
    if (element === null) return;
    held.current = false;
    following.current = false;
    const height = element.clientHeight || view.height;
    const next = scrollFor(offsets[index] ?? 0, offsets[index + 1], element.scrollTop, height);
    element.scrollTop = next;
    const row = drawnRow(element, index);
    if (row === null) pending.current = {index, then};
    else then(row);
    setView({top: next, height});
  };
  const slotOf = (index: number): Slot =>
    slotFor(index, count, keyOf, measured.current, () => setMeasures(n => n + 1));

  const spacers = {before: offsets[first] ?? 0, after: total - (offsets[last] ?? total)};
  return {scroller, scrolled, touched, first, last, ...spacers, slotOf, reveal, behind, jump};
}
