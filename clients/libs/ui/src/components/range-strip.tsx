import {type JSX, type KeyboardEvent, type PointerEvent, useRef} from 'react';
import {SIZE} from '../tokens/index.js';
import {useInlineSize} from './label-fit.js';

/** A window of an axis: from and to, in the axis' own units. */
export type RangeWindow = readonly [number, number];

/**
 * The window a drag chose: the axis `domain` (from, to) drawn `width` px wide, the drag pressed at
 * px `from` and now at px `to`. The drag may go either way and past either end; the window is cut
 * to the domain. A drag shorter than `least` px is a press and not a window: it gives null.
 */
export function windowOfDrag(
  domain: RangeWindow,
  width: number,
  drag: {readonly from: number; readonly to: number},
  least = 3,
): RangeWindow | null {
  if (!(width > 0) || Math.abs(drag.to - drag.from) < least) return null;
  const at = (px: number): number =>
    domain[0] + (Math.min(width, Math.max(0, px)) / width) * (domain[1] - domain[0]);
  const [low, high] = [at(drag.from), at(drag.to)].sort((a, b) => a - b);
  return [low ?? domain[0], high ?? domain[1]];
}

/** The window moved by `by` of its own length, kept inside the domain at the same length. */
export function windowMoved(domain: RangeWindow, window: RangeWindow, by: number): RangeWindow {
  const length = window[1] - window[0];
  const start = Math.min(domain[1] - length, Math.max(domain[0], window[0] + by * length));
  return [start, start + length];
}

/** A point of the line the strip draws: where along the axis, and its height in the line's own units. */
export interface StripPoint {
  readonly x: number;
  readonly y: number;
}

/**
 * A thin overview of a whole axis with the window the reader has chosen marked on it. Dragging
 * across it chooses a window (`change` is called with it); pressing without dragging, or Escape, asks
 * for the whole axis again (`change(null)`); the arrow keys move the window by a tenth of its
 * length, so the strip is no pointer-only control, and Home and End take it to the ends. The line
 * (`steps`) is drawn scaled to the strip: its shape, not its values. `window` is null for the
 * whole axis.
 */
export function RangeStrip(props: {
  readonly label: string;
  readonly domain: RangeWindow;
  readonly window: RangeWindow | null;
  readonly steps: readonly StripPoint[];
  /** The window in words (`Everything`, `Last 6 hours`), which assistive technology reads as the value. */
  readonly valueText: string;
  readonly change: (window: RangeWindow | null) => void;
}): JSX.Element {
  const {domain, window} = props;
  const [measure, measured] = useInlineSize();
  const width = Math.max(1, measured ?? 1);
  const height = SIZE['overview-strip'];
  const drag = useRef<number | null>(null);
  const along = (at: number): number =>
    ((at - domain[0]) / Math.max(1, domain[1] - domain[0])) * width;
  const xOf = (event: PointerEvent<HTMLDivElement>): number =>
    event.clientX - event.currentTarget.getBoundingClientRect().left;
  const ys = props.steps.map(point => point.y);
  const [low, high] = [Math.min(...ys), Math.max(...ys)];
  const inset = 4;
  const yAt = (value: number): number =>
    high === low ? height / 2 : inset + ((high - value) / (high - low)) * (height - 2 * inset);
  const path = props.steps
    .map(
      (point, at) =>
        `${at === 0 ? 'M' : 'H'}${along(point.x)}${at === 0 ? ' ' : ' V'}${yAt(point.y)}`,
    )
    .join(' ');
  const key = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === 'Escape' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      props.change(null);
      return;
    }
    const by = event.key === 'ArrowLeft' ? -0.1 : event.key === 'ArrowRight' ? 0.1 : 0;
    if (by === 0) return;
    event.preventDefault();
    props.change(windowMoved(domain, window ?? domain, by));
  };
  const first = window === null ? domain[0] : window[0];
  return (
    // A strip is a pointer control with a keyboard's equivalent in the arrow keys: its window's
    // start stands as the slider's value, and its words (`valueText`) as what that value means.
    <div
      className="vs-range-strip"
      ref={measure}
      role="slider"
      tabIndex={0}
      aria-label={props.label}
      aria-valuemin={domain[0]}
      aria-valuemax={domain[1]}
      aria-valuenow={first}
      aria-valuetext={props.valueText}
      onKeyDown={key}
      onPointerDown={event => {
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = xOf(event);
      }}
      onPointerMove={event => {
        const from = drag.current;
        if (from === null) return;
        const chosen = windowOfDrag(domain, width, {from, to: xOf(event)});
        if (chosen !== null) props.change(chosen);
      }}
      onPointerUp={event => {
        const from = drag.current;
        drag.current = null;
        if (from !== null && windowOfDrag(domain, width, {from, to: xOf(event)}) === null) {
          props.change(null);
        }
      }}
    >
      <svg
        className="vs-range-strip-plot"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        aria-hidden="true"
      >
        <rect className="vs-range-strip-frame" x={0} y={0} width={width} height={height} />
        {props.steps.length === 0 ? null : <path className="vs-range-strip-line" d={path} />}
        <rect
          className="vs-range-strip-window"
          x={window === null ? 0 : along(window[0])}
          y={0}
          width={window === null ? width : Math.max(3, along(window[1]) - along(window[0]))}
          height={height}
        />
      </svg>
    </div>
  );
}
