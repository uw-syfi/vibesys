import type {JSX, ReactNode} from 'react';

/**
 * The app window's layout, filling its container: a navigation column at the sidebar width (`nav`
 * at the top, `footer` at the bottom) and the main area beside it, which holds a `Page`; each
 * scrolls on its own. On a narrow window the column moves above the main area. The column starts
 * below the window's own controls (`--chrome-inset-height`, which the desktop app sets before the
 * first paint) and its background drags the window.
 */
export function AppFrame(props: {
  /** The navigation column's accessible name. */
  readonly label: string;
  readonly nav: ReactNode;
  readonly footer?: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-app-frame">
      <nav className="vs-app-nav" aria-label={props.label}>
        <div className="vs-app-nav-top">{props.nav}</div>
        {props.footer === undefined ? null : (
          <div className="vs-app-nav-footer">{props.footer}</div>
        )}
      </nav>
      <div className="vs-app-main">{props.children}</div>
    </div>
  );
}
