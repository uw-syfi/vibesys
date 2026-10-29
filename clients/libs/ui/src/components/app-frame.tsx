import {
  type CSSProperties,
  type JSX,
  type PointerEvent,
  type ReactNode,
  useRef,
  useState,
} from 'react';

const DEFAULT_WIDTH = 264;
const MIN_WIDTH = 180;
const MAX_WIDTH = 480;
const COLLAPSE_WIDTH = 120;

/**
 * The app window's layout, filling its container: a navigation column at the sidebar width (`nav`
 * at the top, `footer` at the bottom) and the main area beside it, which holds a `Page`; each
 * scrolls on its own. On a narrow window (under 800 px) the column floats over the rail and the
 * start of the main area, which keeps its place beside the rail. The column starts
 * below the window's own controls (`--chrome-inset-height`, which the desktop app sets before the
 * first paint) and its background drags the window.
 *
 * The title-bar row is the top strip of the window, as high as the window's own controls
 * (`--chrome-inset-height`) and at least a control tall. `control` sits in it, vertically centered
 * and after the window's controls (`--chrome-inset-width`), and stays in the same place whether the
 * column is shown or not. With `navHidden` the column takes no width at all and is gone for
 * assistive technology and the keyboard, and the `rail`, if any, takes its place. The rail is wide
 * enough to hold the window's controls and the toggle, so the page starts at the same place whether
 * the column is shown or not. The column changes size over the `--duration-fast` token, and not at
 * all when the user prefers reduced motion.
 */
export function AppFrame(props: {
  /** The navigation column's accessible name. */
  readonly label: string;
  readonly nav: ReactNode;
  /** Controls that remain visible while the navigation list scrolls. */
  readonly navPinned?: ReactNode;
  readonly footer?: ReactNode;
  /** Hide the navigation column completely. */
  readonly navHidden?: boolean;
  /** The title-bar row's control (the sidebar toggle), shown whether the column is hidden or not. */
  readonly control?: ReactNode;
  /**
   * What stays reachable while the column is hidden: a narrow rail of icon buttons in its place,
   * named `label` for assistive technology. The first child sits at the top, the last at the bottom.
   */
  readonly rail?: {readonly label: string; readonly content: ReactNode};
  readonly onCollapse?: () => void;
  readonly children: ReactNode;
}): JSX.Element {
  const hidden = props.navHidden === true;
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const drag = useRef<{x: number; width: number} | null>(null);
  const move = (event: PointerEvent<HTMLDivElement>): void => {
    if (drag.current === null) return;
    const next = drag.current.width + event.clientX - drag.current.x;
    setWidth(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, next)));
  };
  const end = (event: PointerEvent<HTMLDivElement>): void => {
    const start = drag.current;
    if (start === null) return;
    drag.current = null;
    const next = start.width + event.clientX - start.x;
    if (next < COLLAPSE_WIDTH) props.onCollapse?.();
  };
  return (
    <div
      className="vs-app-frame"
      data-nav-hidden={hidden || undefined}
      style={{'--sidebar-width': `${width}px`} as CSSProperties}
    >
      {props.control === undefined ? null : (
        <div className="vs-app-titlebar-control">{props.control}</div>
      )}
      <nav
        className="vs-app-nav"
        aria-label={props.label}
        data-hidden={hidden || undefined}
        aria-hidden={hidden || undefined}
        inert={hidden}
      >
        {props.navPinned === undefined ? null : (
          <div className="vs-app-nav-pinned">{props.navPinned}</div>
        )}
        <div className="vs-app-nav-top">{props.nav}</div>
        {props.footer === undefined ? null : (
          <div className="vs-app-nav-footer">{props.footer}</div>
        )}
        <hr
          className="vs-app-nav-resize"
          aria-label="Resize sidebar"
          aria-orientation="vertical"
          aria-valuemin={MIN_WIDTH}
          aria-valuemax={MAX_WIDTH}
          aria-valuenow={width}
          tabIndex={hidden ? -1 : 0}
          onPointerDown={event => {
            drag.current = {x: event.clientX, width};
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={move}
          onPointerUp={end}
          onPointerCancel={() => {
            drag.current = null;
          }}
          onKeyDown={event => {
            if (event.key === 'Home') props.onCollapse?.();
            else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
              setWidth(
                Math.max(
                  MIN_WIDTH,
                  Math.min(MAX_WIDTH, width + (event.key === 'ArrowRight' ? 16 : -16)),
                ),
              );
            } else return;
            event.preventDefault();
          }}
        />
      </nav>
      {props.rail !== undefined ? (
        <nav className="vs-app-rail" aria-label={props.rail.label}>
          {props.rail.content}
        </nav>
      ) : null}
      <div className="vs-app-main">
        <div className="vs-app-content">{props.children}</div>
      </div>
    </div>
  );
}

/**
 * A window that is only a page (no navigation column): the same title-bar row as
 * `AppFrame`, empty and draggable, clear of the window's own controls, with `children` below it
 * filling the window.
 */
export function WindowFrame(props: {readonly children: ReactNode}): JSX.Element {
  return (
    <div className="vs-window-frame">
      <div className="vs-app-strip" />
      <div className="vs-app-content">{props.children}</div>
    </div>
  );
}
