import type {JSX, ReactNode} from 'react';

/**
 * The app window's layout, filling its container: a navigation column at the sidebar width (`nav`
 * at the top, `footer` at the bottom) and the main area beside it, which holds a `Page`; each
 * scrolls on its own. On a narrow window the column moves above the main area. The column starts
 * below the window's own controls (`--chrome-inset-height`, which the desktop app sets before the
 * first paint) and its background drags the window.
 *
 * With `navHidden` the column takes no width at all and is gone for assistive technology and the
 * keyboard; the main area then starts with a strip below which the page sits, clear of the window's
 * own controls, holding `reveal` (the control that brings the column back) beside them. The column
 * changes size over the `--duration-fast` token, and not at all when the user prefers reduced
 * motion.
 */
export function AppFrame(props: {
  /** The navigation column's accessible name. */
  readonly label: string;
  readonly nav: ReactNode;
  readonly footer?: ReactNode;
  /** Hide the navigation column completely. */
  readonly navHidden?: boolean;
  /** Shown in the main area's top strip while the column is hidden. */
  readonly reveal?: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  const hidden = props.navHidden === true;
  return (
    <div className="vs-app-frame">
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
        {hidden ? <div className="vs-app-strip">{props.reveal}</div> : null}
        <div className="vs-app-content">{props.children}</div>
      </div>
    </div>
  );
}
