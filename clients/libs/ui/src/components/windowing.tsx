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
 */
export function useWindowed(options: {
  readonly count: number;
  readonly keyOf: (index: number) => string;
  readonly estimate: number;
  readonly follow: boolean;
}): Windowed {
  const {count, keyOf, estimate, follow} = options;
  const following = useRef(follow);
  const pane = useRef<HTMLElement | null>(null);
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
  const top = following.current ? Math.max(0, total - view.height) : view.top;
  const first = Math.max(0, indexAt(offsets, top) - OVERSCAN);
  const last = Math.min(count, indexAt(offsets, top + view.height) + 1 + OVERSCAN);

  const scroller = (element: HTMLElement | null): void => {
    pane.current = element;
    if (element === null) return;
    if (element.clientHeight > 0 && element.clientHeight !== view.height) {
      setView({top: view.top, height: element.clientHeight});
    }
    if (following.current) element.scrollTop = element.scrollHeight;
    const wanted = pending.current;
    if (wanted === null) return;
    const row = element.querySelector<HTMLElement>(`[aria-posinset="${wanted.index + 1}"]`);
    if (row !== null) {
      pending.current = null;
      wanted.then(row);
    }
  };
  const scrolled = (event: UIEvent<HTMLElement>): void => {
    const element = event.currentTarget;
    if (follow) {
      following.current = atBottom(element);
      setBehind(!following.current);
    }
    setView({top: element.scrollTop, height: element.clientHeight || view.height});
  };
  const jump = (): void => {
    following.current = true;
    setBehind(false);
    if (pane.current !== null) pane.current.scrollTop = pane.current.scrollHeight;
  };
  const reveal = (index: number, then: (row: HTMLElement) => void): void => {
    const element = pane.current;
    if (element === null) return;
    following.current = false;
    const height = element.clientHeight || view.height;
    const start = offsets[index] ?? 0;
    const end = offsets[index + 1] ?? start;
    let next = view.top;
    if (start < view.top) next = start;
    else if (end > view.top + height) next = end - height;
    element.scrollTop = next;
    const row = element.querySelector<HTMLElement>(`[aria-posinset="${index + 1}"]`);
    if (row !== null) {
      then(row);
    } else {
      pending.current = {index, then};
    }
    setView({top: next, height});
  };
  const slotOf = (index: number): Slot => ({
    index,
    size: count,
    measure: row => {
      if (row === null) return;
      const height = row.offsetHeight;
      const key = keyOf(index);
      if (height > 0 && measured.current.get(key) !== height) {
        measured.current.set(key, height);
        setMeasures(tick => tick + 1);
      }
    },
  });

  return {
    scroller,
    scrolled,
    first,
    last,
    before: offsets[first] ?? 0,
    after: total - (offsets[last] ?? total),
    slotOf,
    reveal,
    behind,
    jump,
  };
}
