import type {JSX, ReactNode} from 'react';

/**
 * The app window's layout, filling its container: a navigation column at the sidebar width (`nav`
 * at the top, `footer` at the bottom) and the main area beside it, which holds a `Page`; each
 * scrolls on its own. On a narrow window the column moves above the main area. The column starts
 * below the window's own controls (`--chrome-inset-height`, which the desktop app sets before the
 * first paint) and its background drags the window.
 *
 * The title-bar row is the top strip of the window, as high as the window's own controls
 * (`--chrome-inset-height`) and at least a control tall. `control` sits in it, vertically centered
 * and after the window's controls (`--chrome-inset-width`), and stays in the same place whether the
 * column is shown or not. With `navHidden` the column takes no width at all and is gone for
 * assistive technology and the keyboard; the main area then starts with an empty drag strip of the
 * row's height, so the page sits below the row. The column changes size over the `--duration-fast`
 * token, and not at all when the user prefers reduced motion.
 */
export function AppFrame(props: {
  /** The navigation column's accessible name. */
  readonly label: string;
  readonly nav: ReactNode;
  readonly footer?: ReactNode;
  /** Hide the navigation column completely. */
  readonly navHidden?: boolean;
  /** The title-bar row's control (the sidebar toggle), shown whether the column is hidden or not. */
  readonly control?: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  const hidden = props.navHidden === true;
  return (
    <div className="vs-app-frame">
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
        <div className="vs-app-nav-top">{props.nav}</div>
        {props.footer === undefined ? null : (
          <div className="vs-app-nav-footer">{props.footer}</div>
        )}
      </nav>
      <div className="vs-app-main">
        {hidden ? <div className="vs-app-strip" /> : null}
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
